import math
import threading
import time
import numpy as np
MOTORS = tuple((i, "rs-06" if i <= 3 else "rs-00") for i in range(1, 8))


HOST_ID = 0xFD
GAINS = ((50.0, 2.0), (100.0, 4.0), (100.0, 4.0), (40.0, 2.0), (40.0, 1.5), (40.0, 1.5), (12.0, 0.05))
LIMITS_DEG = ((-155.0, 155.0), (-3.0, 175.0), (-3.0, 175.0), (-85.0, 85.0), (-85.0, 85.0), (-179.0, 179.0))
ZERO_TOLERANCE = math.radians(6.0)
TRACKING_LIMIT = math.radians(15.0)
TRACKING_GRACE_S = 0.4
FEEDBACK_TIMEOUT_S = 0.5
TEMPERATURE_LIMIT_C = 80.0
WIDTH_OPEN = 0.05
ACCEL_DEG_S2 = 120.0
STOP_ACCEL_DEG_S2 = 240.0
GRIPPER_ACCEL_DEG_S2 = 300.0
MAX_JOG_DEG_S = 45.0
MAX_TELEOP_DEG_S = 90.0
MAX_TELEOP_ACCEL_DEG_S2 = 480.0
GRIPPER_LEAD_RAD = 3.5 / 12.0
GRIPPER_HOLD_LEAD_RAD = 1.0 / 12.0
GRIPPER_JOG_DEG = (-180.0, 360.0)
GRAVITY_LIMIT_NM = (12.0, 12.0, 12.0, 4.0, 4.0, 4.0)


class RSArm:
    def __init__(self, channel="can0", controller_factory=None, gripper_open_deg=90.0, speed_deg_s=20.0, gripper_speed_deg_s=150.0, rate_hz=200.0, gravity=None, gravity_scale=1.0):
        self.channel = channel
        self.gravity = gravity
        self.gravity_scale = float(gravity_scale)
        self.tau = np.zeros(7)
        self.factory = controller_factory
        self.gripper_open = math.radians(gripper_open_deg)
        self.gripper_closed = 0.0
        self.gripper_stalled = False
        self.default_speeds = np.array([math.radians(speed_deg_s)] * 6 + [math.radians(gripper_speed_deg_s)])
        self.speeds = self.default_speeds.copy()
        self.accel = np.radians([ACCEL_DEG_S2] * 6 + [GRIPPER_ACCEL_DEG_S2])
        self.stop_accel = np.radians([STOP_ACCEL_DEG_S2] * 6 + [GRIPPER_ACCEL_DEG_S2])
        self.braking = False
        self.rate = np.zeros(7)
        self.period = 1.0 / rate_hz
        self.lower = np.radians([low for low, _ in LIMITS_DEG])
        self.upper = np.radians([high for _, high in LIMITS_DEG])
        self.controller = None
        self.motors = []
        self.lock = threading.RLock()
        self.thread = None
        self.running = threading.Event()
        self.measured = np.zeros(7)
        self.command = np.zeros(7)
        self.target = np.zeros(7)
        self.velocity = np.zeros(6)
        self.effort = np.zeros(6)
        self.temperatures = [None] * 7
        self.current = 0.0
        self.connected = False
        self.zeroed = False
        self.torque = False
        self.enabled = False
        self.fault = None
        self.feedback_at = 0.0
        self.track_since = None

    @property
    def q(self):
        with self.lock:
            return self.measured[:6].copy()

    @property
    def width(self):
        with self.lock:
            span = self.gripper_open - self.gripper_closed
            return float(np.clip((self.measured[6] - self.gripper_closed) / span, 0, 1) * WIDTH_OPEN) if span else 0.0

    @property
    def moving(self):
        with self.lock:
            arm = np.max(np.abs(self.target[:6] - self.command[:6])) > 1e-3 or np.max(np.abs(self.rate[:6])) > 1e-3
            gripper = (abs(self.target[6] - self.command[6]) > 1e-3 or abs(self.rate[6]) > 1e-3) and not self.gripper_stalled
            return bool(self.torque and (arm or gripper))

    @moving.setter
    def moving(self, value):
        pass

    def _open(self):
        factory = self.factory
        if factory is None:
            from motorbridge import Controller
            factory = Controller
        return factory(self.channel)

    def connect(self):
        if self.connected:
            return
        controller = self._open()
        try:
            motors = [controller.add_robstride_motor(mid, HOST_ID, model) for mid, model in MOTORS]
            for (mid, _), motor in zip(MOTORS, motors):
                device_id, _ = motor.robstride_ping_host_id(HOST_ID, 700)
                if device_id != mid:
                    raise RuntimeError(f"Motor {mid} did not answer with its own ID")
        except Exception:
            controller.close()
            raise
        self.controller = controller
        self.motors = motors
        self.connected = True
        self.zeroed = False
        self.fault = None
        self.read_positions()

    def read_positions(self):
        if not self.connected:
            raise ValueError("The RS arm is not connected")
        with self.lock:
            values = [motor.robstride_get_param_f32(0x7019, 500) for motor in self.motors]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Position feedback is not finite")
            self.measured = np.array(values, dtype=float)
            self.feedback_at = time.monotonic()
            return self.measured.copy()

    def confirm_zero(self, anywhere=False):
        positions = self.read_positions()
        if anywhere:
            outside = [i + 1 for i, value in enumerate(positions[:6]) if value < self.lower[i] - math.radians(1) or value > self.upper[i] + math.radians(1)]
            if outside:
                raise ValueError("Joints " + ", ".join(map(str, outside)) + " read outside their range; move them by hand into range or check the zero calibration")
            self.zeroed = True
            return positions
        far = [i + 1 for i, value in enumerate(positions) if abs(value) > ZERO_TOLERANCE]
        if far:
            raise ValueError("Joints " + ", ".join(map(str, far)) + " are not at the zero pose; place the arm in its zero pose first")
        self.zeroed = True
        return positions

    def enable(self):
        if not self.connected or not self.zeroed:
            raise ValueError("Connect and confirm the zero pose before enabling motors")
        if self.torque:
            return
        from motorbridge import Mode
        with self.lock:
            for motor in self.motors:
                motor.ensure_mode(Mode.MIT, 1000)
            positions = self.read_positions()
            self.command = positions.copy()
            self.target = positions.copy()
            self.rate = np.zeros(7)
            self.speeds = self.default_speeds.copy()
            self.braking = False
            for motor, position, (kp, kd) in zip(self.motors, positions, GAINS):
                motor.send_mit(float(position), 0.0, kp, kd, 0.0)
            self.controller.enable_all()
            self.torque = True
            self.fault = None
            self.track_since = None
            self.feedback_at = time.monotonic()
        self.running.set()
        self.thread = threading.Thread(target=self._loop, name="rs-mit", daemon=True)
        self.thread.start()

    def _loop(self):
        next_tick = time.perf_counter()
        tick = 0
        while self.running.is_set():
            try:
                with self.lock:
                    self._advance()
                    if self.gravity is not None and self.gravity_scale:
                        torque = np.asarray(self.gravity(self.measured[:6])) * self.gravity_scale
                        self.tau[:6] = np.clip(np.nan_to_num(torque), -np.array(GRAVITY_LIMIT_NM), GRAVITY_LIMIT_NM)
                    for motor, position, rate, tau, (kp, kd) in zip(self.motors, self.command, self.rate, self.tau, GAINS):
                        motor.send_mit(float(position), float(rate), kp, kd, float(tau))
                    self._feedback(tick % 100 == 0)
            except Exception as error:
                self._fail(f"Motor communication failed: {error}")
            tick += 1
            next_tick += self.period
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()

    def _advance(self):
        dt = self.period
        accel = self.stop_accel if self.braking else self.accel
        error = self.target - self.command
        desired = np.sign(error) * np.minimum(self.speeds, np.sqrt(2 * accel * np.abs(error)))
        self.rate += np.clip(desired - self.rate, -accel * dt, accel * dt)
        step = self.rate * dt
        crossed = (np.abs(step) >= np.abs(error)) & (np.sign(step) == np.sign(error))
        settled = (np.abs(error) < 1e-4) & (np.abs(self.rate) < accel * dt * 2)
        done = crossed | settled
        self.command = np.where(done, self.target, self.command + step)
        self.rate = np.where(done, 0.0, self.rate)
        lead = self.command[6] - self.measured[6]
        limit = GRIPPER_HOLD_LEAD_RAD if self.gripper_stalled else GRIPPER_LEAD_RAD
        if abs(lead) > limit:
            self.gripper_stalled = True
            self.command[6] = self.measured[6] + math.copysign(GRIPPER_HOLD_LEAD_RAD, lead)
            self.rate[6] = 0.0
        elif abs(self.target[6] - self.command[6]) > GRIPPER_HOLD_LEAD_RAD:
            self.gripper_stalled = False
        if self.braking and not np.any(self.rate[:6]) and np.allclose(self.command[:6], self.target[:6]):
            self.braking = False
            self.speeds = self.default_speeds.copy()

    def _feedback(self, temperatures):
        now = time.monotonic()
        fresh = False
        for index, motor in enumerate(self.motors):
            state = motor.get_state()
            if state is None or not math.isfinite(state.pos):
                continue
            self.measured[index] = state.pos
            fresh = True
            if index < 6:
                self.velocity[index] = state.vel if math.isfinite(state.vel) else 0.0
                self.effort[index] = state.torq if math.isfinite(state.torq) else 0.0
            else:
                self.current = abs(state.torq) if math.isfinite(state.torq) else 0.0
            if temperatures and math.isfinite(state.t_mos):
                self.temperatures[index] = round(state.t_mos, 1)
        if fresh:
            self.feedback_at = now
        elif now - self.feedback_at > FEEDBACK_TIMEOUT_S:
            self._fail("Motor feedback stopped arriving")
            return
        if any(t is not None and t > TEMPERATURE_LIMIT_C for t in self.temperatures):
            self._fail("A motor is above the temperature limit")
            return
        error = np.abs(self.measured[:6] - self.command[:6])
        if np.max(error) > TRACKING_LIMIT:
            if self.track_since is None:
                self.track_since = now
            elif now - self.track_since > TRACKING_GRACE_S:
                joint = int(np.argmax(error)) + 1
                self._fail(f"Joint {joint} is not following its command; check for a collision or obstruction")
        else:
            self.track_since = None

    def _fail(self, reason):
        with self.lock:
            if self.fault is None:
                self.fault = reason
            self.enabled = False
            self.command = self.measured.copy()
            self.target = self.measured.copy()
            self.rate = np.zeros(7)
            self.braking = False

    def set_gripper_range(self, closed, opened):
        if not (math.isfinite(closed) and math.isfinite(opened)) or abs(opened - closed) < math.radians(10):
            raise ValueError("Open and closed must be at least 10 degrees apart")
        with self.lock:
            self.gripper_closed = float(closed)
            self.gripper_open = float(opened)

    def check_limits(self, q):
        q = np.asarray(q, dtype=float)
        if q.shape != (6,) or not np.isfinite(q).all():
            raise ValueError("Six finite joint positions are required")
        for index, value in enumerate(q):
            if value < self.lower[index] - 1e-6 or value > self.upper[index] + 1e-6:
                raise ValueError(f"Joint {index + 1} limit reached")

    def write(self, q, dt):
        if not self.torque or not self.enabled or self.fault:
            raise ValueError(self.fault or "Arm is not ready")
        self.check_limits(q)
        with self.lock:
            self.braking = False
            self.speeds = self.default_speeds.copy()
            self.target[:6] = np.asarray(q, dtype=float)

    def grip(self, width, contact=False):
        if not self.torque or not self.enabled or self.fault:
            raise ValueError(self.fault or "Arm is not enabled")
        if not np.isfinite(width) or not 0 <= width <= 0.057:
            raise ValueError("Gripper aperture limit reached")
        with self.lock:
            self.braking = False
            self.speeds[6] = self.default_speeds[6]
            self.target[6] = float(self.gripper_closed + min(width, WIDTH_OPEN) / WIDTH_OPEN * (self.gripper_open - self.gripper_closed))

    def set_speed(self, speed_deg_s, accel_deg_s2=ACCEL_DEG_S2):
        speed = math.radians(min(float(speed_deg_s), MAX_TELEOP_DEG_S))
        with self.lock:
            self.accel[:6] = math.radians(min(float(accel_deg_s2), MAX_TELEOP_ACCEL_DEG_S2))
            self.default_speeds[:6] = speed
            if not self.braking:
                self.speeds[:6] = speed

    def jog(self, joint, direction, speed):
        if not self.torque or not self.enabled or self.fault:
            raise ValueError(self.fault or "Arm is not ready")
        if joint not in range(7) or direction not in (-1, 1):
            raise ValueError("Choose a joint and a direction")
        speed = min(abs(float(speed)), math.radians(MAX_JOG_DEG_S))
        with self.lock:
            self.braking = False
            self.speeds = self.default_speeds.copy()
            self.speeds[joint] = speed
            self.target = self.command.copy()
            if joint == 6:
                self.target[6] = math.radians(GRIPPER_JOG_DEG[1] if direction > 0 else GRIPPER_JOG_DEG[0])
            else:
                self.target[joint] = self.upper[joint] if direction > 0 else self.lower[joint]

    def stop(self):
        with self.lock:
            stopping = self.rate * np.abs(self.rate) / (2 * self.stop_accel)
            self.target = np.clip(self.command + stopping, np.append(self.lower, -np.inf), np.append(self.upper, np.inf))
            self.braking = True
            self.velocity[:] = 0
        self.enabled = False

    def at_rest(self):
        with self.lock:
            return bool(np.max(np.abs(self.measured[:6])) < ZERO_TOLERANCE)

    def release(self):
        self.stop()
        self.running.clear()
        if self.thread:
            self.thread.join(timeout=1)
        with self.lock:
            if self.controller is not None and self.torque:
                self.controller.disable_all()
            self.torque = False

    def close(self):
        self.stop()
        self.running.clear()
        if self.thread:
            self.thread.join(timeout=1)
        with self.lock:
            controller, self.controller = self.controller, None
            self.motors = []
            self.connected = False
            self.zeroed = False
            if controller is not None:
                controller.close()

    def status(self):
        with self.lock:
            return {
                "kind": "rs", "channel": self.channel, "connected": self.connected, "zero_confirmed": self.zeroed,
                "torque": self.torque, "fault": self.fault, "at_rest": self.connected and self.at_rest(),
                "temperatures": list(self.temperatures), "gravity_nm": [round(float(v), 2) for v in self.tau[:6]], "gravity_scale": self.gravity_scale,
                "tracking_deg": [round(math.degrees(float(v)), 1) for v in self.measured[:6] - self.command[:6]], "gripper_deg": round(math.degrees(self.measured[6]), 1), "gripper_range_deg": [round(math.degrees(self.gripper_closed), 1), round(math.degrees(self.gripper_open), 1)],
                "limits_deg": [list(pair) for pair in LIMITS_DEG], "feedback_age_ms": round((time.monotonic() - self.feedback_at) * 1000) if self.feedback_at else None,
            }
