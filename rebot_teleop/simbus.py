import math
import threading
import time
from types import SimpleNamespace
import numpy as np
from rebot_teleop import config as C

MOTOR_TAU_S = 0.02


class SimMotor:
    def __init__(self, bus, index, motor_id):
        self.bus = bus
        self.index = index
        self.id = motor_id

    def robstride_ping_host_id(self, host, timeout_ms=1000):
        if self.id in self.bus.dead:
            raise TimeoutError("no answer")
        return self.id, host

    def robstride_get_param_f32(self, param, timeout_ms=1000):
        with self.bus.lock:
            self.bus.step()
            if param == 0x700B:
                return C.PEAK_NM["rs-06" if self.id <= 3 else "rs-00"]
            return float(self.bus.pos[self.index])

    def robstride_write_param_f32(self, param, value):
        pass

    def ensure_mode(self, mode, timeout_ms=1000):
        pass

    def clear_error(self):
        self.bus.codes.pop(self.id, None)

    def enable(self):
        pass

    def robstride_get_fault_report(self):
        return 0, 0

    def send_mit(self, pos, vel, kp, kd, tau):
        with self.bus.lock:
            self.bus.goal[self.index] = pos
            self.bus.sent += 1

    def get_state(self):
        with self.bus.lock:
            self.bus.step()
            return SimpleNamespace(pos=float(self.bus.pos[self.index]), vel=float(self.bus.vel[self.index]), torq=0.0, status_code=self.bus.codes.get(self.id, 0), t_mos=self.bus.temperature)


class SimBus:
    def __init__(self, channel, start_deg=None):
        self.channel = channel
        self.lock = threading.RLock()
        self.pos = np.radians(np.asarray(start_deg if start_deg is not None else [0.0] * 6 + [C.GRIPPER_OPEN_DEG], dtype=float))
        self.goal = self.pos.copy()
        self.vel = np.zeros(7)
        self.enabled = False
        self.dead = set()
        self.codes = {}
        self.at = time.monotonic()
        self.sent = 0
        self.temperature = 34.0
        self.motors = []

    def step(self):
        now = time.monotonic()
        dt = min(now - self.at, 0.1)
        self.at = now
        if not self.enabled or dt <= 0:
            self.vel[:] = 0.0
            return
        alpha = 1.0 - math.exp(-dt / MOTOR_TAU_S)
        moved = (self.goal - self.pos) * alpha
        self.vel = moved / dt
        self.pos = self.pos + moved

    def add_robstride_motor(self, motor_id, feedback_id, model):
        motor = SimMotor(self, motor_id - 1, motor_id)
        self.motors.append(motor)
        return motor

    def enable_all(self):
        with self.lock:
            self.enabled = True
            self.at = time.monotonic()

    def disable_all(self):
        with self.lock:
            self.enabled = False

    def close(self):
        pass

    def move_by_hand(self, joints_deg):
        with self.lock:
            self.pos[: len(joints_deg)] = np.radians(joints_deg)
            self.goal = self.pos.copy()


class SimFactory:
    def __init__(self, starts=None):
        self.starts = starts or {}
        self.buses = {}

    def __call__(self, channel):
        if channel not in self.buses:
            self.buses[channel] = SimBus(channel, self.starts.get(channel))
        return self.buses[channel]
