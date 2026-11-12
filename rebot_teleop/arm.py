import collections
import math
import threading
import time
import traceback
import numpy as np
from rebot_teleop import config as C
from rebot_teleop import heat as H

POSITION_PARAM = 0x7019
TORQUE_LIMIT_PARAM = 0x700B
LOWER = np.radians([low for low, _ in C.LIMITS_DEG])
UPPER = np.radians([high for _, high in C.LIMITS_DEG])
SLACK = math.radians(C.LIMIT_SLACK_DEG)
SERVO_MAX = np.radians(list(C.SERVO_MAX_DEG_S) + [C.GRIPPER_DEG_S])
SERVO_ACCEL = math.radians(C.SERVO_MAX_DEG_S2)
GRIPPER_LEAD = math.radians(C.GRIPPER_LEAD_DEG)
GRIPPER_HOLD_LEAD = math.radians(C.GRIPPER_HOLD_LEAD_DEG)


def min_jerk(start, goal, speed, accel, dt):
    start = np.asarray(start, dtype=float)
    delta = np.asarray(goal, dtype=float) - start
    distance = float(np.max(np.abs(delta))) if delta.size else 0.0
    if distance < 1e-5:
        return [(0.0, start + delta)]
    duration = max(0.25, 1.875 * distance / speed, math.sqrt(5.774 * distance / accel))
    count = max(2, math.ceil(duration / dt))
    out = []
    for t in np.linspace(0.0, duration, count + 1):
        u = t / duration
        out.append((float(t), start + delta * (10 * u**3 - 15 * u**4 + 6 * u**5)))
    return out


STATUS_BITS = ("undervoltage", "overcurrent", "over temperature", "encoder fault", "stall overload", "not calibrated")
LOAD_WINDOW_S = 10.0
KP = np.array([kp for kp, _ in C.GAINS[:6]])
PEAK = np.array([C.PEAK_NM[model] for _, model in C.MOTORS])
PUSH_LEAD = C.PUSH_FRACTION * PEAK[:6] / KP
RATED = np.array([C.RATED_NM[model] for _, model in C.MOTORS])
HOLD_LEAD = RATED[:6] / KP
NO_GRIPPER_ALERT = "Gripper motor 7 is not answering. This arm is running without its gripper. After fixing the cable, press Power on to pick it up"


class Arm:
    def __init__(self, name, channel, gravity=None, controller_factory=None, motors=None):
        self.name = name
        self.motor_list = H.motor_list(motors)
        self.models = [model for _, model in self.motor_list[:6]]
        self.peak = np.array([C.PEAK_NM[model] for _, model in self.motor_list])
        self.rated = np.array([C.RATED_NM[model] for _, model in self.motor_list])
        self.push_lead = C.PUSH_FRACTION * self.peak[:6] / KP
        self.hold_lead = self.rated[:6] / KP
        self.continuous = np.array([H.continuous(m) for m in self.models])
        self.heat = H.Budget(self.models)
        self.heat_at = 0.0
        self.on_event = None
        self.relief = None
        self.quick2 = np.zeros(6)
        self.lower = LOWER.copy()
        self.upper = UPPER.copy()
        self.channel = channel
        self.gravity = gravity
        self.gravity_scale = 1.0
        self.factory = controller_factory
        self.lock = threading.RLock()
        self.connect_lock = threading.Lock()
        self.controller = None
        self.motors = []
        self.connected = False
        self.torque = False
        self.has_gripper = True
        self.spare = None
        self.answering = []
        self.codes = [None] * 7
        self.code_logged = 0.0
        self.faulted = set()
        self.load2 = np.zeros(7)
        self.load_at = 0.0
        self.pushing = np.zeros(6)
        self.blocked = np.zeros(6, dtype=bool)
        self.rest_phase = None
        self.rest_return = None
        self.rest_resume = C.HEAT_RESUME
        self.rests = 0
        self.rest_reason = None
        self.rest_at = 0.0
        self.resting = False
        self.catches = []
        self.history = collections.deque(maxlen=int(C.TORQUE_HISTORY_S / C.TORQUE_BUCKET_S))
        self.bucket = np.zeros(7)
        self.bucket_at = 0.0
        self.offset = np.zeros(7)
        self.position_known = False
        self.turn_fixes = 0
        self.measured = np.zeros(7)
        self.velocity = np.zeros(7)
        self.effort = np.zeros(7)
        self.temperatures = [None] * 7
        self.torque_limits = [None] * 7
        self.command = np.zeros(7)
        self.rate = np.zeros(7)
        self.target = np.zeros(7)
        self.tau = np.zeros(7)
        self.trajectory = None
        self.trajectory_start = 0.0
        self.gripper_closed = math.radians(C.GRIPPER_CLOSED_DEG)
        self.gripper_open = math.radians(C.GRIPPER_OPEN_DEG)
        self.gripper_stalled = False
        self.fault = None
        self.cooling = False
        self.home_q = None
        self.note = None
        self.feedback_at = 0.0
        self.running = threading.Event()
        self.thread = None
        self.period = 1.0 / C.LOOP_HZ
        self.loop_hz = 0.0

    def _open(self):
        if self.factory is not None:
            return self.factory(self.channel)
        from motorbridge import Controller
        return Controller(self.channel)

    def connect(self):
        with self.connect_lock:
            if self.connected:
                return True
            return self._connect()

    def _log(self, text):
        print(f"{time.strftime('%H:%M:%S')} {self.name} arm: {text}", flush=True)
        if self.on_event is not None:
            try:
                self.on_event(self.name, text)
            except Exception:
                pass

    def _pose_text(self):
        return "joints " + " ".join(f"{math.degrees(v):.0f}" for v in self.measured[:6]) + " deg, torque " + " ".join(f"{v:.1f}" for v in self.effort[:6]) + " Nm"

    def _shut(self, controller, motors):
        for motor in motors:
            try:
                motor.close()
            except Exception:
                pass
        controller.close()

    def _connect(self):
        controller = self._open()
        motors, answering = [], []
        try:
            for mid, model in self.motor_list:
                motor = controller.add_robstride_motor(mid, C.HOST_ID, model)
                motors.append(motor)
            for (mid, _), motor in zip(self.motor_list, motors):
                try:
                    device, _ = motor.robstride_ping_host_id(C.HOST_ID, 150)
                    if device == mid:
                        answering.append(mid)
                except Exception:
                    pass
        except Exception as error:
            self._shut(controller, motors)
            self.answering = []
            self.note = f"CAN {self.channel}: {error}"
            return False
        if answering != self.answering:
            self._log(f"motors answering {answering}")
        self.answering = answering
        no_gripper = answering == [m for m, _ in self.motor_list[:6]]
        if len(answering) < len(self.motor_list) and not no_gripper:
            self._shut(controller, motors)
            missing = [m for m, _ in self.motor_list if m not in answering]
            if not answering:
                self.note = f"No motors answer on {self.channel}; is the arm power on?"
            else:
                self.note = "Motors " + ", ".join(map(str, missing)) + f" do not answer on {self.channel}; check the cable after motor {min(missing) - 1}"
            return False
        if no_gripper:
            self._log("running without the gripper")
        with self.lock:
            self.controller = controller
            self.motors = motors[:6] if no_gripper else motors
            self.spare = motors[6] if no_gripper else None
            self.has_gripper = not no_gripper
            self.connected = True
            self.note = None
            self.fault = None
        try:
            previous = self._known()
            self.position_known = False
            self.offset = np.zeros(7)
            self.offset = self._wrap_offsets(self.read_positions(follow=False), previous)
            self.read_positions()
            for index, motor in enumerate(motors):
                try:
                    self.torque_limits[index] = round(float(motor.robstride_get_param_f32(TORQUE_LIMIT_PARAM, 200)), 2)
                except Exception:
                    self.torque_limits[index] = None
        except Exception as error:
            self.close()
            self.note = f"Could not read the joints on {self.channel}: {error}"
            return False
        return True

    def _report_fault(self, index, motor):
        try:
            fault, warning = motor.robstride_get_fault_report()
            self._log(f"motor {index + 1} fault report {fault:#x}, warning {warning:#x}")
        except Exception as error:
            self._log(f"motor {index + 1} fault report failed: {error}")

    def _motor_fault(self, index, motor, code):
        names = ", ".join(name for bit, name in enumerate(STATUS_BITS) if code >> bit & 1) or f"code {code}"
        joint = "Gripper" if index == 6 else f"Joint {index + 1}"
        load = math.sqrt(self.load2[index])
        text = f"{joint} motor cut out: {names}. It averaged {load:.0f} Nm over the last {LOAD_WINDOW_S:.0f} s and can hold {self._still_nm(index):.0f} Nm for long when still. Let the arm hang, then press Power on to reset it"
        error = math.degrees(self.command[index] - self.measured[index])
        recent = [values[index] for _, values in list(self.history)[-50:]] if index < 6 else []
        if recent:
            self._log(f"motor {index + 1} torque over the last 5 s: highest {max(recent):.1f} Nm, half second peaks " + " ".join(f"{max(recent[i:i + 5]):.0f}" for i in range(0, len(recent), 5)))
        self._log(f"{text}; last torque {self.effort[index]:.1f} Nm, position error {error:.1f} deg; {self._pose_text()}")
        self.faulted.add(index)
        if not self.cooling:
            self.fault = text
        threading.Thread(target=self._report_fault, args=(index, motor), daemon=True).start()
        now = time.monotonic()
        self.catches = [t for t in self.catches if now - t < C.CATCH_WINDOW_S]
        if self.torque and index < 6 and len(self.catches) < C.CATCH_LIMIT:
            self.catches.append(now)
            threading.Thread(target=self._catch, args=(index, f"{joint} motor cut out: {names}"), daemon=True).start()

    def _catch(self, index, what):
        with self.lock:
            if not self.torque or index >= len(self.motors):
                return
            try:
                self.motors[index].clear_error()
            except Exception as error:
                self._log(f"could not catch motor {index + 1}: {error}")
                return
            self.trajectory = None
            self.rate = np.zeros(7)
            self.command[:6] = self.measured[:6]
            self.target[:6] = np.clip(self.measured[:6], self.lower, self.upper)
        time.sleep(0.03)
        with self.lock:
            if not self.torque or index >= len(self.motors):
                return
            self.command[:6] = self.measured[:6]
            self.target[:6] = np.clip(self.measured[:6], self.lower, self.upper)
        time.sleep(0.01)
        with self.lock:
            if not self.torque or index >= len(self.motors):
                return
            try:
                self.motors[index].enable()
            except Exception as error:
                self._log(f"could not catch motor {index + 1}: {error}")
                return
            self.codes[index] = None
            self.resting = True
            self.rest_at = time.monotonic()
            self.rest_phase = "lowering"
            self.rest_return = None
            self.rest_reason = what
            if not self.cooling:
                self.fault = f"{what}. It was caught and the arm is pulling the hand in. Press Power on to carry on"
            self._relieve()
        self._log(f"caught motor {index + 1}; relaxing at home")

    def _still_nm(self, index):
        return float(self.continuous[index]) if index < 6 else float(self.rated[index])

    def _watch_load(self, dt):
        now = time.monotonic()
        self.heat_at = now
        self.heat.step(np.abs(self.effort[:6]), np.degrees(np.abs(self.velocity[:6])), dt)
        self.quick2 += (self.effort[:6] ** 2 - self.quick2) * min(dt / 0.3, 1.0)
        if self.resting:
            self._rest_step(now)
            return
        if not self.torque or self.cooling or self.fault:
            return
        over = [i for i in range(6) if self.heat.used[i] >= self.heat.limit and self.heat.rates[i] > 0]
        if over:
            worst = max(over, key=lambda i: self.heat.used[i])
            self._log(f"joint {worst + 1} used up its budget holding {abs(self.effort[worst]):.1f} Nm; relaxing at home; {self._pose_text()}")
            self._start_rest(f"Joint {worst + 1} used up what it can hold", C.COOL_HOME_DEG_S, come_back=C.REST_COMES_BACK)

    def _start_rest(self, reason, speed_deg_s, resume=None, come_back=True):
        self.rests += 1
        self.resting = True
        self.rest_at = time.monotonic()
        self.rest_phase = "lowering"
        self.rest_reason = reason
        self.rest_resume = C.HEAT_RESUME if resume is None else resume
        self.rest_return = self.command[:6].copy() if come_back else None
        self._rest_fault()
        self._relieve(speed_deg_s)

    def _rest_eta(self):
        return max(0.0, float(np.max(self.heat.used)) - self.rest_resume) * C.HEAT_COOL_S

    def _rest_fault(self):
        if self.cooling:
            return
        if self.faulted:
            return
        if self.rest_phase == "lowering":
            self.fault = f"{self.rest_reason}, so the arm is pulling the hand in until every joint can hold its load"
        elif self.rest_phase == "recovering" and self.rest_return is not None:
            self.fault = f"{self.rest_reason}. Resting here for about {self._rest_eta():.0f} s, then it goes back"
        elif self.rest_phase == "recovering":
            self.fault = f"{self.rest_reason}. It cannot pull in further from here; bring it in or down, or support it"
        elif self.rest_phase == "returning":
            self.fault = f"{self.rest_reason}. Recovered, going back"

    def _holdable(self, fraction=None):
        fraction = C.HEAT_HOLD_MARGIN if fraction is None else fraction
        return bool(np.all(np.sqrt(self.quick2) <= fraction * self.continuous))

    def _rest_step(self, now):
        if self.faulted:
            return
        if self.rest_phase == "lowering":
            if self.trajectory is not None and self._holdable(C.RELIEF_STOP_FRACTION) and now - self.rest_at > C.REST_SETTLE_S:
                self.trajectory = None
                self.target[:6] = self.command[:6]
                self.rate = np.zeros(7)
                self._log(f"pulled in far enough for the motors to recover; {self._pose_text()}")
            if self.trajectory is None:
                self.rest_phase = "recovering"
                if self.rest_return is None and self._holdable():
                    self._finish_rest("in range; carry on while the motors recover")
                    return
        if self.rest_phase == "recovering":
            recovered = float(np.max(self.heat.used)) <= self.rest_resume
            if self.rest_return is None and (recovered or self._holdable()):
                self._finish_rest("light enough now; carry on")
                return
            if self.rest_return is not None and recovered:
                self.rest_phase = "returning"
                self._log("recovered; going back")
                self._plan(self.rest_return, C.REST_RETURN_DEG_S)
        elif self.rest_phase == "returning" and self.trajectory is None:
            self._finish_rest("back where it was; ready again")
        self._rest_fault()

    def _finish_rest(self, text):
        self.resting = False
        self.rest_phase = None
        self.rest_return = None
        if not self.cooling:
            self.fault = None
        self._log(text)

    def relax(self, reason="Resting so the motors can recover", resume=None, speed_deg_s=None, come_back=True):
        with self.lock:
            if not self.torque or self.resting:
                return False
            self._start_rest(reason, speed_deg_s or C.MOVE_DEG_S / 2, resume, come_back)
        self._log(f"relaxing: {reason}; {self._pose_text()}")
        return True

    def rest_status(self):
        if not self.resting:
            return None
        return {"phase": self.rest_phase, "eta_s": round(self._rest_eta()) if self.rest_phase == "recovering" else None, "comes_back": self.rest_return is not None, "reason": self.rest_reason}

    def _end_rest(self):
        with self.lock:
            if not self.resting:
                return
            self.resting = False
            self.rest_phase = None
            self.rest_return = None
            self.trajectory = None
            self.target[:6] = self.command[:6]
            self.rate = np.zeros(7)
            if not self.cooling:
                self.fault = None

    def _reset_motors(self):
        with self.lock:
            bad = sorted(i for i in self.faulted if i < len(self.motors))
            if not bad:
                return
            for index in bad:
                try:
                    self.motors[index].clear_error()
                except Exception as error:
                    self._log(f"motor {index + 1} would not clear its fault: {error}")
            self.command[:len(self.motors)] = self.measured[:len(self.motors)]
            self.target[:6] = np.clip(self.measured[:6], self.lower, self.upper)
            self.rate = np.zeros(7)
            self.trajectory = None
        time.sleep(0.05)
        with self.lock:
            for index in bad:
                self.motors[index].enable()
                self.load2[index] = 0.0
            self.faulted.clear()
            self.codes = [None] * 7
            if not self.cooling:
                self.fault = None
        self._log("reset motor " + ", ".join(str(i + 1) for i in bad) + " after its fault; holding where the arm is")

    def _heating(self):
        if not self.torque:
            return []
        left = self.heat.seconds_left()
        return [(i + 1, float(self.heat.used[i]), left[i]) for i in range(6) if self.heat.used[i] >= C.HEAT_WARN and left[i] is not None and i not in self.faulted]

    def _alert_tags(self):
        return (["no gripper"] if self.connected and not self.has_gripper else []) + (["heating"] if self._heating() else []) + (["blocked"] if self.torque and self.blocked.any() else [])

    def _alert(self):
        parts = []
        if self.connected and not self.has_gripper:
            parts.append(NO_GRIPPER_ALERT)
        for joint, used, left in self._heating():
            parts.append(f"Joint {joint} has used {used * 100:.0f}% of what it can hold in this pose. In {left:.0f} s the arm lowers itself, so bring the arm in or down")
        if self.torque and self.blocked.any():
            parts.append("Joint " + ", ".join(str(i + 1) for i in np.flatnonzero(self.blocked)) + " is pressing on something, so it eased off. Pull back to free it")
        return ". ".join(parts) or None

    def _gripper_back(self):
        try:
            device, _ = self.spare.robstride_ping_host_id(C.HOST_ID, 150)
            return device == self.motor_list[6][0]
        except Exception:
            return False

    def _known(self):
        return self.measured[:6].copy() if self.position_known else None

    def _wrap_offsets(self, raw, previous=None):
        offsets = np.zeros(7)
        for index in range(6):
            low, high = LOWER[index] - SLACK, UPPER[index] + SLACK
            fits = [turn for turn in (0.0, 2 * math.pi, -2 * math.pi, 4 * math.pi, -4 * math.pi) if low <= raw[index] + turn <= high]
            if not fits:
                continue
            if previous is not None and len(fits) > 1:
                offsets[index] = min(fits, key=lambda turn: abs(raw[index] + turn - previous[index]))
            else:
                offsets[index] = fits[0]
        low = min(self.gripper_closed, self.gripper_open) - math.radians(30)
        high = max(self.gripper_closed, self.gripper_open) + math.radians(30)
        if not low <= raw[6] <= high:
            for turn in (2 * math.pi, -2 * math.pi):
                if low <= raw[6] + turn <= high:
                    offsets[6] = turn
                    break
        return offsets

    def _follow(self, index, raw):
        value = raw + self.offset[index]
        if index >= 6 or not self.position_known:
            return value
        turns = round((self.measured[index] - value) / (2 * math.pi))
        if not turns:
            return value
        shift = turns * 2 * math.pi
        self.offset[index] += shift
        self.turn_fixes += 1
        self._log(f"joint {index + 1} reading jumped {math.degrees(-shift):+.0f} deg in one sample; treating it as the same position ({math.degrees(value):.0f} is {math.degrees(value + shift):.0f})")
        return value + shift

    def read_positions(self, follow=True):
        with self.lock:
            values = [motor.robstride_get_param_f32(POSITION_PARAM, 300) for motor in self.motors]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("Position feedback is not finite")
            measured = self.measured.copy()
            for index, value in enumerate(values):
                measured[index] = self._follow(index, float(value)) if follow else float(value) + self.offset[index]
            self.measured = measured
            if follow:
                self.position_known = True
            self.feedback_at = time.monotonic()
            return self.measured.copy()

    def refresh(self):
        if self.torque or not self.connected:
            return self.measured.copy()
        try:
            return self.read_positions()
        except Exception as error:
            self.note = f"Lost the arm on {self.channel}: {error}"
            self.close()
            return self.measured.copy()

    def _raise_torque_limits(self):
        for index, ((_, model), motor) in enumerate(zip(self.motor_list, self.motors)):
            limit = self.torque_limits[index]
            if limit is not None and limit < C.PEAK_NM[model] - 0.05:
                try:
                    motor.robstride_write_param_f32(TORQUE_LIMIT_PARAM, C.PEAK_NM[model])
                    self.torque_limits[index] = C.PEAK_NM[model]
                except Exception:
                    pass

    def enable(self):
        if self.connected and not self.torque and not self.has_gripper and self._gripper_back():
            self._log("the gripper motor answers again")
            self.close()
        if not self.connected and not self.connect():
            raise ValueError(self.note or f"{self.name} arm is not reachable")
        if self.torque:
            self._reset_motors()
            self._end_rest()
            return
        try:
            from motorbridge import Mode
            mit = Mode.MIT
        except ImportError:
            mit = None
        with self.lock:
            previous = self._known()
            self.position_known = False
            self.offset = np.zeros(7)
            self.offset = self._wrap_offsets(self.read_positions(follow=False), previous)
            q = self.read_positions()
            for motor in self.motors:
                try:
                    motor.clear_error()
                except Exception:
                    pass
            for motor in self.motors:
                motor.ensure_mode(mit, 1000)
            self._raise_torque_limits()
            self.command = q.copy()
            self.target = q.copy()
            self.target[:6] = np.clip(q[:6], self.lower, self.upper)
            self.rate = np.zeros(7)
            self.trajectory = None
            if not self.cooling:
                self.fault = None
            self.codes = [None] * 7
            self.faulted.clear()
            self.load2 = np.zeros(7)
            self.pushing = np.zeros(6)
            self.blocked = np.zeros(6, dtype=bool)
            if self.heat_at:
                self.heat.cool(time.monotonic() - self.heat_at)
            self.heat_at = time.monotonic()
            self.quick2 = np.zeros(6)
            self.resting = False
            self.catches = []
            self.gripper_stalled = False
            for motor, position, offset, (kp, kd) in zip(self.motors, q, self.offset, C.GAINS):
                motor.send_mit(float(position - offset), 0.0, kp, kd, 0.0)
            self.controller.enable_all()
            self.torque = True
            self.feedback_at = time.monotonic()
        self._log("torque on")
        self.running.set()
        self.thread = threading.Thread(target=self._loop, name=f"arm-{self.name}", daemon=True)
        self.thread.start()

    def disable(self):
        if self.torque:
            callers = " < ".join(f.name for f in reversed(traceback.extract_stack(limit=7)[:-1]))
            self._log(f"torque off, called from {callers}; fault {self.fault}; note {self.note}")
        self.running.clear()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        self.thread = None
        with self.lock:
            if self.controller is not None and self.torque:
                try:
                    self.controller.disable_all()
                except Exception:
                    pass
            self.torque = False
            self.trajectory = None
            self.rate = np.zeros(7)

    def close(self):
        if self.connected:
            self._log(f"closing the CAN connection; note {self.note}")
        self.disable()
        with self.lock:
            controller, self.controller = self.controller, None
            motors, self.motors = self.motors, []
            if self.spare is not None:
                motors.append(self.spare)
                self.spare = None
            self.connected = False
        if controller is not None:
            try:
                self._shut(controller, motors)
            except Exception:
                pass

    def _loop(self):
        next_tick = time.perf_counter()
        ticks, window = 0, time.perf_counter()
        while self.running.is_set():
            try:
                with self.lock:
                    self._advance(time.monotonic())
                    if self.gravity is not None and self.gravity_scale:
                        torque = np.nan_to_num(np.asarray(self.gravity(self.measured[:6]), dtype=float)) * self.gravity_scale
                        self.tau[:6] = np.clip(torque, -self.peak[:6], self.peak[:6])
                    frames = [(m, float(p - o), float(r), kp, kd, float(t)) for m, p, o, r, t, (kp, kd) in zip(self.motors, self.command, self.offset, self.rate, self.tau, C.GAINS)]
                for motor, position, rate, kp, kd, tau in frames:
                    motor.send_mit(position, rate, kp, kd, tau)
                with self.lock:
                    self._feedback(ticks % 100 == 0)
            except Exception as error:
                message = f"CAN error on {self.channel}: {error}"
                if self.note != message:
                    self._log(message)
                self.note = message
                time.sleep(0.05)
            ticks += 1
            now = time.perf_counter()
            if now - window >= 1.0:
                self.loop_hz = ticks / (now - window)
                ticks, window = 0, now
            next_tick += self.period
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()

    def _advance(self, now):
        dt = self.period
        if self.trajectory is not None:
            times, points = self.trajectory
            t = now - self.trajectory_start
            if t >= times[-1]:
                self.command[:6] = points[-1]
                self.target[:6] = points[-1]
                self.rate[:6] = 0.0
                self.trajectory = None
            else:
                i = int(np.searchsorted(times, t, side="right"))
                i = min(max(i, 1), len(times) - 1)
                u = (t - times[i - 1]) / max(times[i] - times[i - 1], 1e-6)
                q = points[i - 1] + (points[i] - points[i - 1]) * u
                self.rate[:6] = (q - self.command[:6]) / dt
                self.command[:6] = q
        else:
            error = self.target[:6] - self.command[:6]
            desired = np.clip(error / C.SERVO_TAU_S, -SERVO_MAX[:6], SERVO_MAX[:6])
            self.rate[:6] += np.clip(desired - self.rate[:6], -SERVO_ACCEL * dt, SERVO_ACCEL * dt)
            step = self.rate[:6] * dt
            overshoot = (np.abs(step) > np.abs(error)) & (np.sign(step) == np.sign(error))
            self.command[:6] = np.where(overshoot, self.target[:6], self.command[:6] + step)
            self.rate[:6] = np.where(overshoot | (np.abs(error) < 1e-5), 0.0, self.rate[:6])
        self._limit_push(dt)
        error = self.target[6] - self.command[6]
        step = float(np.clip(error, -SERVO_MAX[6] * dt, SERVO_MAX[6] * dt))
        self.command[6] += step
        self.rate[6] = step / dt
        lead = self.command[6] - self.measured[6]
        limit = GRIPPER_HOLD_LEAD if self.gripper_stalled else GRIPPER_LEAD
        if abs(lead) > limit:
            self.gripper_stalled = True
            self.command[6] = self.measured[6] + math.copysign(GRIPPER_HOLD_LEAD, lead)
            self.rate[6] = 0.0
        elif abs(self.target[6] - self.command[6]) > GRIPPER_HOLD_LEAD * 2:
            self.gripper_stalled = False

    def _limit_push(self, dt):
        if not self.feedback_at:
            return
        lead = self.command[:6] - self.measured[:6]
        limit = np.where(self.blocked, self.hold_lead, self.push_lead)
        over = np.abs(lead) > limit
        if over.any():
            self.command[:6] = np.where(over, self.measured[:6] + np.sign(lead) * limit, self.command[:6])
            self.rate[:6] = np.where(over, 0.0, self.rate[:6])
        self.rate[:6] = np.clip(self.rate[:6], -SERVO_MAX[:6], SERVO_MAX[:6])
        self.pushing = np.where(over, self.pushing + dt, 0.0)
        newly = (self.pushing > C.BLOCKED_AFTER_S) & ~self.blocked
        if newly.any():
            self.blocked |= newly
            self._log("joint " + ", ".join(str(i + 1) for i in np.flatnonzero(newly)) + " is blocked; easing off to a force it can hold")
        freed = self.blocked & (np.abs(self.target[:6] - self.measured[:6]) <= self.hold_lead)
        if freed.any():
            self.blocked &= ~freed
            self._log("joint " + ", ".join(str(i + 1) for i in np.flatnonzero(freed)) + " is free again")

    def torque_history(self):
        with self.lock:
            rows = list(self.history)
        return {"t": [t for t, _ in rows], "nm": [values for _, values in rows], "rated": self.continuous.tolist(), "peak": self.peak[:6].tolist()}

    def _feedback(self, temperatures):
        now = time.monotonic()
        fresh = False
        dt = min(max(now - self.load_at, 0.0), 0.1) if self.load_at else 0.0
        blend = min(dt / LOAD_WINDOW_S, 1.0)
        self.load_at = now
        for index, motor in enumerate(self.motors):
            state = motor.get_state()
            if state is None or not math.isfinite(state.pos):
                continue
            fresh = True
            code = getattr(state, "status_code", None)
            if code != self.codes[index]:
                self.codes[index] = code
                if code:
                    self._motor_fault(index, motor, code)
                elif now - self.code_logged > 0.25:
                    self.code_logged = now
                    self._log(f"motor {index + 1} status code {code}")
            self.measured[index] = self._follow(index, float(state.pos))
            self.velocity[index] = state.vel if math.isfinite(state.vel) else 0.0
            self.effort[index] = state.torq if math.isfinite(state.torq) else 0.0
            self.load2[index] += (self.effort[index] ** 2 - self.load2[index]) * blend
            if temperatures and math.isfinite(state.t_mos):
                self.temperatures[index] = round(float(state.t_mos), 1)
        self.bucket = np.maximum(self.bucket, np.abs(self.effort))
        if now - self.bucket_at >= C.TORQUE_BUCKET_S:
            self.history.append((round(time.time(), 2), [round(float(v), 2) for v in self.bucket[:6]]))
            self.bucket = np.zeros(7)
            self.bucket_at = now
        self._watch_load(dt)
        if fresh:
            self.feedback_at = now
            if self.note and self.note.startswith("No feedback"):
                self._log("feedback is back")
                self.note = None
        elif now - self.feedback_at > C.FEEDBACK_TIMEOUT_S:
            if not (self.note and self.note.startswith("No feedback")):
                self._log("no feedback from any motor")
            self.note = f"No feedback from the {self.name} arm for {now - self.feedback_at:.1f} s; check its power and CAN cable"
        if temperatures:
            self._watch_temperature()

    def _watch_temperature(self):
        known = [(i + 1, t) for i, t in enumerate(self.temperatures) if t is not None]
        hot = [(j, t) for j, t in known if t >= C.TEMPERATURE_LIMIT_C]
        if hot and not self.cooling:
            joint, t = max(hot, key=lambda h: h[1])
            self.cooling = True
            self.fault = f"Over temperature: joint {joint} at {t:.0f} C. Pulling the hand in to cool; usable again below {C.COOLED_C:.0f} C"
            self._relieve()
        elif self.cooling:
            peak = max((t for _, t in known), default=0.0)
            if peak < C.COOLED_C:
                self.cooling = False
                self.fault = None
            elif self.trajectory is None:
                self.fault = f"Over temperature: resting, {peak:.0f} C now, usable again below {C.COOLED_C:.0f} C"

    def _relieve(self, speed_deg_s=None):
        self.rate[:] = 0.0
        path = None
        if self.relief is not None and self.torque:
            try:
                path = self.relief(self.command[:6].copy())
            except Exception as error:
                self._log(f"could not plan a pull in: {error}")
        if path is None or len(path) < 2:
            self.trajectory = None
            self.target = self.command.copy()
            return False
        speed = math.radians(speed_deg_s or C.COOL_HOME_DEG_S)
        points = np.clip(np.asarray(path, dtype=float)[:, :6], self.lower, self.upper)
        times = [0.0]
        for a, b in zip(points[:-1], points[1:]):
            times.append(times[-1] + max(0.02, float(np.max(np.abs(b - a))) / speed))
        self.trajectory = (np.array(times), points)
        self.trajectory_start = time.monotonic()
        self.target[:6] = points[-1]
        return True

    def _plan(self, goal, speed_deg_s):
        goal = np.clip(np.asarray(goal, dtype=float)[:6], self.lower, self.upper)
        self.rate[:] = 0.0
        samples = min_jerk(self.command[:6], goal, math.radians(speed_deg_s), math.radians(speed_deg_s * 6), 0.01)
        self.trajectory = (np.array([t for t, _ in samples]), np.array([q for _, q in samples]))
        self.trajectory_start = time.monotonic()
        self.target[:6] = goal

    def hold(self):
        with self.lock:
            if self.resting:
                self.rest_return = None
                if self.rest_phase == "returning":
                    self.rest_phase = "recovering"
                    self.rest_resume = 1.0
            self.trajectory = None
            self.target = self.command.copy()
            self.rate[:] = 0.0

    def _check(self):
        if not self.torque:
            raise ValueError(f"The {self.name} arm is not powered")
        if self.fault:
            raise ValueError(self.fault)

    def servo(self, q):
        q = np.clip(np.asarray(q, dtype=float)[:6], self.lower, self.upper)
        with self.lock:
            self._check()
            if self.trajectory is not None:
                self.trajectory = None
            self.target[:6] = q

    def play(self, samples):
        self._check()
        times = np.array([t for t, _ in samples], dtype=float)
        points = np.clip(np.array([q for _, q in samples], dtype=float)[:, :6], self.lower, self.upper)
        with self.lock:
            self.rate[:6] = 0.0
            self.trajectory = (times, points)
            self.trajectory_start = time.monotonic()
            self.target[:6] = points[-1]
        return float(times[-1])

    def move(self, q, speed_deg_s=C.MOVE_DEG_S, accel_deg_s2=C.MOVE_DEG_S2):
        start = self.command[:6].copy() if self.torque else self.measured[:6].copy()
        goal = np.clip(np.asarray(q, dtype=float)[:6], self.lower, self.upper)
        samples = min_jerk(start, goal, math.radians(speed_deg_s), math.radians(accel_deg_s2), 0.01)
        return self.play(samples)

    @property
    def busy(self):
        with self.lock:
            return self.trajectory is not None

    def width_to_angle(self, width):
        u = float(np.clip(width, 0.0, C.GRIPPER_WIDTH_M)) / C.GRIPPER_WIDTH_M
        return self.gripper_closed + u * (self.gripper_open - self.gripper_closed)

    @property
    def width(self):
        span = self.gripper_open - self.gripper_closed
        return float(np.clip((self.measured[6] - self.gripper_closed) / span, 0, 1) * C.GRIPPER_WIDTH_M) if span else 0.0

    def grip(self, width):
        with self.lock:
            self._check()
            if not self.has_gripper:
                return
            goal = self.width_to_angle(width)
            if abs(goal - self.target[6]) > GRIPPER_HOLD_LEAD:
                self.gripper_stalled = False
            self.target[6] = goal

    def set_gripper_range(self, closed, opened):
        if abs(opened - closed) < math.radians(10):
            raise ValueError("Open and closed must be at least 10 degrees apart")
        self.gripper_closed, self.gripper_open = float(closed), float(opened)

    def gripper_is(self, which):
        goal = self.gripper_open if which == "open" else self.gripper_closed
        with self.lock:
            raw = self.measured[6] - self.offset[6]
            shift = round((goal - raw) / (2 * math.pi)) * 2 * math.pi - self.offset[6]
            self.offset[6] += shift
            self.measured[6] += shift
            self.command[6] += shift
            self.target[6] += shift
        return float(self.measured[6])

    def clear_fault(self):
        hot = [t for t in self.temperatures if t is not None and t >= C.COOLED_C]
        if hot:
            raise ValueError(f"Still {max(hot):.0f} C; usable again below {C.COOLED_C:.0f} C")
        self.cooling = False
        self.fault = None
        self._reset_motors()
        self._end_rest()

    @property
    def q(self):
        return self.measured[:6].copy()

    def status(self):
        with self.lock:
            age = time.monotonic() - self.feedback_at if self.feedback_at else None
            warm = [(i + 1, t) for i, t in enumerate(self.temperatures) if t is not None and t >= C.TEMPERATURE_WARN_C]
            return {
                "name": self.name, "channel": self.channel, "connected": self.connected, "powered": self.torque,
                "motors_answering": list(self.answering), "fault": self.fault, "cooling": self.cooling, "note": self.note,
                "alert": self._alert(), "alert_tags": self._alert_tags(), "has_gripper": self.has_gripper, "resting": self.resting,
                "heat": self.heat.summary() if self.torque else None, "rest": self.rest_status(), "motors": list(self.models),
                "load_nm": [round(float(v), 1) for v in np.sqrt(self.load2[:6])], "rated_nm": self.rated[:6].tolist(), "hold_nm": self.continuous.tolist(),
                "joints_deg": [round(math.degrees(v), 2) for v in self.measured[:6]],
                "command_deg": [round(math.degrees(v), 2) for v in self.command[:6]],
                "velocity_deg_s": [round(math.degrees(v), 1) for v in self.velocity[:6]],
                "effort_nm": [round(float(v), 2) for v in self.effort[:6]],
                "gravity_nm": [round(float(v), 2) for v in self.tau[:6]],
                "temperatures_c": list(self.temperatures), "torque_limits_nm": list(self.torque_limits),
                "warm": (f"Joint {max(warm, key=lambda w: w[1])[0]} at {max(warm, key=lambda w: w[1])[1]:.0f} C" if warm else None),
                "gripper_deg": round(math.degrees(self.measured[6]), 1), "gripper_m": round(self.width, 4),
                "gripper_range_deg": [round(math.degrees(self.gripper_closed), 1), round(math.degrees(self.gripper_open), 1)],
                "busy": self.trajectory is not None, "loop_hz": round(self.loop_hz), "feedback_age_ms": None if age is None else round(age * 1000),
            }
