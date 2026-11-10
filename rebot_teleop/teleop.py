import math
import threading
import time
import numpy as np
import pinocchio as pin
from rebot_teleop import config as C

XR_TO_ROBOT = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
VIEWS = {"behind": 0.0, "left": 90.0, "front": 180.0, "right": 270.0}
FRESH_S = 0.25
RATE_HZ = 125.0
ENGAGE = 0.5


def yaw_about_up(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def about_z(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def quat_xyzw(q):
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if not n or not math.isfinite(n):
        return None
    return pin.Quaternion(w / n, x / n, y / n, z / n).toRotationMatrix()


class Hand:
    def __init__(self, name):
        self.name = name
        self.sample = None
        self.engaged = False
        self.reference = None
        self.solution = None
        self.reach_error = 0.0
        self.last_width = None
        self.velocity = None


class Teleop:
    def __init__(self, robot, lift=None):
        self.robot = robot
        self.lift = lift
        self.lock = threading.Lock()
        self.hands = {name: Hand(name) for name in robot.units}
        self.active = False
        self.source = None
        self.settings = C.load("teleop.json", {"view": "behind", "scale": 1.0, "rotation": True, "follow_gaze": True})
        self.started_at = 0.0
        self.loop_hz = 0.0
        self.input_hz = 0.0
        self.inputs = 0
        self.latency_ms = None
        self.thread = threading.Thread(target=self._loop, name="teleop", daemon=True)
        self.thread.start()
        robot.listeners.append(self.release_all)

    def configure(self, **values):
        for key in ("view", "scale", "rotation", "follow_gaze"):
            if key in values and values[key] is not None:
                self.settings[key] = values[key]
        if self.settings["view"] not in VIEWS:
            self.settings["view"] = "behind"
        self.settings["scale"] = float(np.clip(float(self.settings["scale"]), 0.2, 3.0))
        C.save("teleop.json", self.settings)
        return self.settings

    def start(self, source):
        powered = self.robot.ensure_powered()
        with self.lock:
            self.active = True
            self.source = source
            self.started_at = time.monotonic()
            for hand in self.hands.values():
                hand.engaged = False
                hand.reference = None
        return {"active": True, "source": source, "powered": powered, "offline": [n for n in self.robot.units if n not in powered]}

    def stop(self):
        with self.lock:
            self.active = False
            self.source = None
        self.release_all()
        if self.lift:
            self.lift.stop()
        return {"active": False}

    def release_all(self):
        with self.lock:
            for hand in self.hands.values():
                hand.engaged = False
                hand.reference = None
                hand.velocity = None

    def push_hands(self, message, source="quest"):
        now = time.monotonic()
        if not self.active:
            self.start(source)
        sent = message.get("t")
        yaw = float(message.get("yaw", 0.0) or 0.0)
        with self.lock:
            self.inputs += 1
            for name, data in (message.get("hands") or {}).items():
                if name not in self.hands or not isinstance(data, dict):
                    continue
                position = data.get("p")
                rotation = quat_xyzw(data["q"]) if data.get("q") else None
                sample = {
                    "at": now, "yaw": yaw, "grip": float(data.get("grip", 0.0) or 0.0), "trigger": float(data.get("trigger", 0.0) or 0.0),
                    "position": None if position is None else np.asarray(position, dtype=float), "rotation": rotation, "stick": data.get("stick") or [0.0, 0.0],
                    "buttons": data.get("buttons") or {},
                }
                self.hands[name].sample = sample
        if self.lift and "left" in (message.get("hands") or {}):
            left = message["hands"]["left"]
            buttons = left.get("buttons") or {}
            stick = left.get("stick") or [0.0, 0.0]
            if buttons.get("x") and abs(stick[1]) > 0.6:
                self.lift.hold("up" if stick[1] < 0 else "down")
            else:
                self.lift.release()
        return sent

    def push_velocity(self, arm, linear, angular, grip=None, clutch=True):
        if not self.active:
            self.start("phone")
        now = time.monotonic()
        with self.lock:
            hand = self.hands.get(arm)
            if hand is None:
                raise ValueError(f"Unknown arm {arm}")
            self.inputs += 1
            hand.velocity = {"at": now, "linear": np.asarray(linear, dtype=float), "angular": np.asarray(angular, dtype=float), "clutch": bool(clutch)}
            if grip is not None:
                hand.sample = {"at": now, "trigger": 1.0 - float(grip), "grip": 0.0, "position": None, "rotation": None, "yaw": 0.0, "stick": [0, 0], "buttons": {}}

    def _frame(self, yaw):
        view = about_z(VIEWS.get(self.settings.get("view", "behind"), 0.0))
        gaze = yaw_about_up(-yaw) if self.settings.get("follow_gaze", True) else np.eye(3)
        return view @ XR_TO_ROBOT @ gaze

    def _loop(self):
        period = 1.0 / RATE_HZ
        next_tick = time.perf_counter()
        ticks, window, inputs_at = 0, time.perf_counter(), 0
        while True:
            try:
                if self.active:
                    self._step(time.monotonic(), period)
            except Exception as error:
                print("teleop step failed:", error, flush=True)
            ticks += 1
            now = time.perf_counter()
            if now - window >= 1.0:
                self.loop_hz = ticks / (now - window)
                self.input_hz = (self.inputs - inputs_at) / (now - window)
                inputs_at = self.inputs
                ticks, window = 0, now
            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()

    def _step(self, now, dt):
        for name, hand in self.hands.items():
            unit = self.robot.units[name]
            arm = unit.arm
            with self.lock:
                sample = hand.sample
                velocity = hand.velocity
            fresh = sample is not None and now - sample["at"] < FRESH_S
            if not unit.ready:
                hand.engaged = False
                continue
            if fresh:
                width = C.GRIPPER_WIDTH_M * (1.0 - float(np.clip(sample["trigger"], 0.0, 1.0)))
                if hand.last_width is None or abs(width - hand.last_width) > 0.0008:
                    try:
                        arm.grip(width)
                        hand.last_width = width
                    except ValueError:
                        pass
            if velocity is not None and now - velocity["at"] < FRESH_S and velocity["clutch"]:
                self._velocity_step(hand, unit, velocity, dt)
                continue
            if not fresh or sample["position"] is None or sample["rotation"] is None or sample["grip"] < ENGAGE:
                hand.engaged = False
                hand.reference = None
                continue
            if not hand.engaged:
                tip, tip_rot = unit.kin.pose(arm.command[:6])
                hand.reference = (sample["position"].copy(), sample["rotation"].copy(), tip, tip_rot, self._frame(sample["yaw"]))
                hand.solution = arm.command[:6].copy()
                hand.engaged = True
            p0, r0, tip, tip_rot, frame = hand.reference
            delta = frame @ (sample["position"] - p0) * float(self.settings.get("scale", 1.0))
            goal_p = tip + delta
            if self.settings.get("rotation", True):
                turn = frame @ (sample["rotation"] @ r0.T) @ frame.T
                goal_r = turn @ tip_rot
            else:
                goal_r = tip_rot
            q, err, _ = unit.kin.ik(goal_p, goal_r, hand.solution, iterations=12)
            hand.solution = q
            hand.reach_error = err
            try:
                arm.servo(q)
            except ValueError:
                hand.engaged = False

    def _velocity_step(self, hand, unit, velocity, dt):
        arm = unit.arm
        if hand.solution is None or not hand.engaged:
            hand.solution = arm.command[:6].copy()
            hand.engaged = True
        tip, rot = unit.kin.pose(hand.solution)
        frame = about_z(VIEWS.get(self.settings.get("view", "behind"), 0.0))
        goal_p = tip + frame @ velocity["linear"] * dt
        spin = frame @ velocity["angular"] * dt
        goal_r = pin.exp3(spin) @ rot
        q, err, _ = unit.kin.ik(goal_p, goal_r, hand.solution, iterations=8)
        hand.solution = q
        hand.reach_error = err
        try:
            arm.servo(q)
        except ValueError:
            hand.engaged = False

    def status(self):
        return {
            "active": self.active, "source": self.source, "settings": self.settings, "loop_hz": round(self.loop_hz), "input_hz": round(self.input_hz),
            "hands": {n: {"engaged": h.engaged, "reach_error_cm": round(h.reach_error * 100, 1), "age_ms": None if h.sample is None else round((time.monotonic() - h.sample["at"]) * 1000)} for n, h in self.hands.items()},
        }
