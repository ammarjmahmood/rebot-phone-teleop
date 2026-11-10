import math
import threading
import time
import numpy as np
from rebot_teleop import config as C

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


class Arm:
    def __init__(self, name, channel, gravity=None, controller_factory=None):
        self.name = name
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
        self.answering = []
        self.offset = np.zeros(7)
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

    def _connect(self):
        controller = self._open()
        motors, answering = [], []
        try:
            for mid, model in C.MOTORS:
                motor = controller.add_robstride_motor(mid, C.HOST_ID, model)
                motors.append(motor)
            for (mid, _), motor in zip(C.MOTORS, motors):
                try:
                    device, _ = motor.robstride_ping_host_id(C.HOST_ID, 150)
                    if device == mid:
                        answering.append(mid)
                except Exception:
                    pass
        except Exception as error:
            controller.close()
            self.answering = []
            self.note = f"CAN {self.channel}: {error}"
            return False
        self.answering = answering
        if len(answering) < len(C.MOTORS):
            controller.close()
            missing = [m for m, _ in C.MOTORS if m not in answering]
            if not answering:
                self.note = f"No motors answer on {self.channel}; is the arm power on?"
            else:
                self.note = "Motors " + ", ".join(map(str, missing)) + f" do not answer on {self.channel}; check the cable after motor {min(missing) - 1}"
            return False
        with self.lock:
            self.controller = controller
            self.motors = motors
            self.connected = True
            self.note = None
            self.fault = None
        try:
            self.offset = np.zeros(7)
            self.offset = self._wrap_offsets(self.read_positions())
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

    def _wrap_offsets(self, raw):
        offsets = np.zeros(7)
        for index in range(6):
            low, high = LOWER[index] - SLACK, UPPER[index] + SLACK
            if low <= raw[index] <= high:
                continue
            for turn in (2 * math.pi, -2 * math.pi, 4 * math.pi, -4 * math.pi):
                if low <= raw[index] + turn <= high:
                    offsets[index] = turn
                    break
        low = min(self.gripper_closed, self.gripper_open) - math.radians(30)
        high = max(self.gripper_closed, self.gripper_open) + math.radians(30)
        if not low <= raw[6] <= high:
            for turn in (2 * math.pi, -2 * math.pi):
                if low <= raw[6] + turn <= high:
                    offsets[6] = turn
                    break
        return offsets

    def read_positions(self):
        with self.lock:
            values = [motor.robstride_get_param_f32(POSITION_PARAM, 300) for motor in self.motors]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("Position feedback is not finite")
            self.measured = np.array(values, dtype=float) + self.offset
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
        for index, ((_, model), motor) in enumerate(zip(C.MOTORS, self.motors)):
            limit = self.torque_limits[index]
            if limit is not None and limit < C.PEAK_NM[model] - 0.05:
                try:
                    motor.robstride_write_param_f32(TORQUE_LIMIT_PARAM, C.PEAK_NM[model])
                    self.torque_limits[index] = C.PEAK_NM[model]
                except Exception:
                    pass

    def enable(self):
        if not self.connected and not self.connect():
            raise ValueError(self.note or f"{self.name} arm is not reachable")
        if self.torque:
            return
        try:
            from motorbridge import Mode
            mit = Mode.MIT
        except ImportError:
            mit = None
        with self.lock:
            self.offset = np.zeros(7)
            self.offset = self._wrap_offsets(self.read_positions())
            q = self.read_positions()
            for motor in self.motors:
                motor.ensure_mode(mit, 1000)
            self._raise_torque_limits()
            self.command = q.copy()
            self.target = q.copy()
            self.target[:6] = np.clip(q[:6], LOWER, UPPER)
            self.rate = np.zeros(7)
            self.trajectory = None
            if not self.cooling:
                self.fault = None
            self.gripper_stalled = False
            for motor, position, offset, (kp, kd) in zip(self.motors, q, self.offset, C.GAINS):
                motor.send_mit(float(position - offset), 0.0, kp, kd, 0.0)
            self.controller.enable_all()
            self.torque = True
            self.feedback_at = time.monotonic()
        self.running.set()
        self.thread = threading.Thread(target=self._loop, name=f"arm-{self.name}", daemon=True)
        self.thread.start()

    def disable(self):
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
        self.disable()
        with self.lock:
            controller, self.controller = self.controller, None
            self.motors = []
            self.connected = False
        if controller is not None:
            try:
                controller.close()
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
                        self.tau[:6] = np.clip(torque, -np.array(C.GRAVITY_LIMIT_NM), C.GRAVITY_LIMIT_NM)
                    frames = [(m, float(p - o), float(r), kp, kd, float(t)) for m, p, o, r, t, (kp, kd) in zip(self.motors, self.command, self.offset, self.rate, self.tau, C.GAINS)]
                for motor, position, rate, kp, kd, tau in frames:
                    motor.send_mit(position, rate, kp, kd, tau)
                with self.lock:
                    self._feedback(ticks % 100 == 0)
            except Exception as error:
                self.note = f"CAN error on {self.channel}: {error}"
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

    def _feedback(self, temperatures):
        now = time.monotonic()
        fresh = False
        for index, motor in enumerate(self.motors):
            state = motor.get_state()
            if state is None or not math.isfinite(state.pos):
                continue
            fresh = True
            self.measured[index] = state.pos + self.offset[index]
            self.velocity[index] = state.vel if math.isfinite(state.vel) else 0.0
            self.effort[index] = state.torq if math.isfinite(state.torq) else 0.0
            if temperatures and math.isfinite(state.t_mos):
                self.temperatures[index] = round(float(state.t_mos), 1)
        if fresh:
            self.feedback_at = now
            if self.note and self.note.startswith("No feedback"):
                self.note = None
        elif now - self.feedback_at > C.FEEDBACK_TIMEOUT_S:
            self.note = f"No feedback from the {self.name} arm for {now - self.feedback_at:.1f} s; check its power and CAN cable"
        if temperatures:
            self._watch_temperature()

    def _watch_temperature(self):
        known = [(i + 1, t) for i, t in enumerate(self.temperatures) if t is not None]
        hot = [(j, t) for j, t in known if t >= C.TEMPERATURE_LIMIT_C]
        if hot and not self.cooling:
            joint, t = max(hot, key=lambda h: h[1])
            self.cooling = True
            self.fault = f"Over temperature: joint {joint} at {t:.0f} C. Going home to cool; usable again below {C.COOLED_C:.0f} C"
            self._go_home()
        elif self.cooling:
            peak = max((t for _, t in known), default=0.0)
            if peak < C.COOLED_C:
                self.cooling = False
                self.fault = None
            elif self.trajectory is None:
                self.fault = f"Over temperature: resting at home, {peak:.0f} C now, usable again below {C.COOLED_C:.0f} C"

    def _go_home(self):
        self.rate[:] = 0.0
        if self.home_q is None or not self.torque:
            self.trajectory = None
            self.target = self.command.copy()
            return
        goal = np.clip(np.asarray(self.home_q, dtype=float)[:6], LOWER, UPPER)
        samples = min_jerk(self.command[:6], goal, math.radians(C.COOL_HOME_DEG_S), math.radians(C.MOVE_DEG_S2), 0.01)
        self.trajectory = (np.array([t for t, _ in samples]), np.array([q for _, q in samples]))
        self.trajectory_start = time.monotonic()
        self.target[:6] = goal

    def hold(self):
        with self.lock:
            self.trajectory = None
            self.target = self.command.copy()
            self.rate[:] = 0.0

    def _check(self):
        if not self.torque:
            raise ValueError(f"The {self.name} arm is not powered")
        if self.fault:
            raise ValueError(self.fault)

    def servo(self, q):
        q = np.clip(np.asarray(q, dtype=float)[:6], LOWER, UPPER)
        with self.lock:
            self._check()
            if self.trajectory is not None:
                self.trajectory = None
            self.target[:6] = q

    def play(self, samples):
        self._check()
        times = np.array([t for t, _ in samples], dtype=float)
        points = np.clip(np.array([q for _, q in samples], dtype=float)[:, :6], LOWER, UPPER)
        with self.lock:
            self.rate[:6] = 0.0
            self.trajectory = (times, points)
            self.trajectory_start = time.monotonic()
            self.target[:6] = points[-1]
        return float(times[-1])

    def move(self, q, speed_deg_s=C.MOVE_DEG_S, accel_deg_s2=C.MOVE_DEG_S2):
        start = self.command[:6].copy() if self.torque else self.measured[:6].copy()
        goal = np.clip(np.asarray(q, dtype=float)[:6], LOWER, UPPER)
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
