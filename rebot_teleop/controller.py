import asyncio
import time
import numpy as np
import pinocchio as pin
from rebot_teleop.drive import CartesianDrive


VIEWS = {"behind": 0.0, "left": 90.0, "front": 180.0, "right": 270.0}
UP = np.array([0.0, 1.0, 0.0])
XR_BEHIND = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
ARKIT_CENTER = np.array([0.07, 0.0, 0.0])
FRESH = {"app": 0.25, "quest": 0.3, "hebi": 0.15}
SETTLE_SAMPLES = 2
MAX_REACH_M = 0.5
CALIBRATION_MIN_M = 0.05
GRIPPER_OPEN_M = 0.05


def view_matrix(view):
    angle = np.radians(VIEWS.get(view, 0.0))
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def orthonormal(forward, left, up):
    up = np.asarray(up, dtype=float) / np.linalg.norm(up)
    forward = np.asarray(forward, dtype=float) - np.dot(forward, up) * up
    if np.linalg.norm(forward) < 0.3:
        return None
    forward /= np.linalg.norm(forward)
    left = np.asarray(left, dtype=float) - np.dot(left, up) * up - np.dot(left, forward) * forward
    if np.linalg.norm(left) < 0.3:
        return None
    return np.array([forward, left / np.linalg.norm(left), up])


class TeleopController:
    def __init__(self, node, name, source, frame):
        if frame not in {"align", "webxr", "calibrated"}:
            raise ValueError("Unknown frame mode")
        self.node = node
        self.name = name
        self.source = source
        self.frame = frame
        self.fresh_s = FRESH.get(name, 0.2)
        self.period = 0.02
        self.drive = CartesianDrive(node)
        self.view = node.settings.get(f"{name}_view", "behind")
        self.use_rotation = True
        self.scale = 1.0
        self.user_axes = None
        saved = node.settings.get(f"{name}_axes") if frame == "calibrated" else None
        self.calibrated_axes = np.array(saved, dtype=float) if saved else None
        self.calibrating = None
        self.calibration = {}
        self.calibration_start = None
        self.calibration_last = None
        self.task = None
        self.owner = None
        self.principal = None
        self.mode = None
        self.reference = None
        self.pressed_samples = 0
        self.align_pending = False
        self.buttons = {}
        self.last_width = None
        self.note = None

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    @property
    def axes(self):
        if self.frame == "webxr":
            return view_matrix(self.view) @ XR_BEHIND
        if self.frame == "align":
            return None if self.user_axes is None else view_matrix(self.view) @ self.user_axes
        return self.calibrated_axes

    async def start(self, principal):
        node = self.node
        if not node.arm.torque:
            raise ValueError("Power the arm on first")
        for other in node.controllers.values():
            if other is not self and other.active:
                await other.stop("Switching controllers", keep_source=True)
        self.source.start(node.settings.get("hebi_addresses", []) if self.name == "hebi" else ())
        generation = await node.takeover(principal)
        node.arm.set_speed(90.0, 480.0)
        self.owner = principal.id
        self.principal = principal
        self.mode = None
        self.note = None
        sample = self.source.latest()
        self.buttons = {key: bool(sample[3].get(key)) for key in ("b2", "b3", "b8")} if sample else {}
        if self.frame == "align":
            self.align_pending = True
            try:
                self.align()
            except ValueError:
                pass
        self.task = asyncio.create_task(self.loop(generation))

    async def stop(self, reason="Controller stopped", keep_source=False):
        if self.task and not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.release()
        if not keep_source:
            self.source.stop()
        self.node.arm.set_speed(20.0, 120.0)
        self.task = None
        self.calibrating = None
        if self.owner and self.node.owner == self.owner:
            await self.node.stop(reason)
        self.owner = None

    def set_view(self, view):
        if view not in VIEWS:
            raise ValueError("Choose behind, front, left or right")
        self.view = view
        self.node.settings.set(f"{self.name}_view", view)

    def align(self):
        sample = self.source.latest()
        if sample is None or time.monotonic() - sample[0] > 1.0:
            raise ValueError("No pose yet; keep the camera uncovered")
        look = sample[2] @ np.array([0.0, 0.0, -1.0])
        forward = look - np.dot(look, UP) * UP
        if np.linalg.norm(forward) < 0.3:
            raise ValueError("Hold the phone upright with the camera looking ahead, then align again")
        forward /= np.linalg.norm(forward)
        self.user_axes = np.array([forward, np.cross(UP, forward), UP])
        self.align_pending = False
        self.note = "Aligned"

    def calibrate(self, step):
        if self.frame != "calibrated":
            raise ValueError("Only HEBI uses motion calibration")
        if step not in {"up", "forward", "left"}:
            raise ValueError("Calibrate up, forward or left")
        if not self.active:
            raise ValueError("Start the controller first")
        self.release()
        self.calibrating = step
        self.calibration_start = None
        self.note = f"Hold Move and move the phone about 10 cm {'straight up' if step == 'up' else 'the way the gripper reaches' if step == 'forward' else 'to the left'}, then let go"

    def finish_calibration(self, displacement):
        if np.linalg.norm(displacement) < CALIBRATION_MIN_M:
            self.note = "That move was too short; move at least 5 cm"
            return
        self.calibration[self.calibrating] = displacement / np.linalg.norm(displacement)
        self.calibrating = None
        missing = [step for step in ("up", "forward", "left") if step not in self.calibration]
        if missing:
            self.note = "Now calibrate " + " and ".join(missing)
            return
        axes = orthonormal(self.calibration["forward"], self.calibration["left"], self.calibration["up"])
        if axes is None:
            self.calibration.pop("forward")
            self.calibration.pop("left")
            self.note = "Those moves were not at right angles; calibrate forward and left again"
            return
        self.calibrated_axes = axes
        self.node.settings.set(f"{self.name}_axes", axes.tolist())
        self.calibration = {}
        self.note = "Directions saved"
        self.source.send({"vibrate": True})

    def release(self):
        if self.mode is not None:
            self.drive.end()
        self.mode = None
        self.reference = None

    async def loop(self, generation):
        try:
            while generation == self.node.generation and self.node.owner == self.owner:
                self.step(time.monotonic())
                await asyncio.sleep(self.period)
        finally:
            self.release()

    def set_gripper(self, width):
        arm = self.node.arm
        if arm.fault or self.node.estopped or not arm.torque:
            return
        arm.enabled = True
        arm.grip(width)
        self.last_width = width

    def step(self, now):
        before = self.note
        try:
            self.advance(now)
        finally:
            if self.note != before and self.name == "hebi":
                self.source.send({"text": self.note or "Ready. Hold Move.", "vibrate": bool(self.note and self.mode)})

    def advance(self, now):
        node = self.node
        sample = self.source.latest()
        fresh = sample is not None and now - sample[0] < self.fresh_s
        inputs = sample[3] if fresh else {}
        rising = {key for key in ("b2", "b3", "b8") if inputs.get(key) and not self.buttons.get(key)}
        self.buttons = {key: bool(inputs.get(key)) for key in ("b2", "b3", "b8")}
        if fresh:
            node.heartbeat_at = now
        if not fresh or node.arm.fault or node.estopped:
            self.release()
            return
        _, position, rotation, inputs = sample
        if self.name == "app":
            position = position + rotation @ ARKIT_CENTER
        if self.align_pending:
            try:
                self.align()
            except ValueError as error:
                self.note = str(error)
        down = bool(inputs.get("b1", 0))
        if self.calibrating:
            if down:
                if self.calibration_start is None:
                    self.calibration_start = position.copy()
                self.calibration_last = position.copy()
            elif self.calibration_start is not None:
                start, self.calibration_start = self.calibration_start, None
                self.finish_calibration(self.calibration_last - start)
            return
        if "grip" in inputs:
            width = float(np.clip(inputs["grip"], 0, 1)) * GRIPPER_OPEN_M
            if self.last_width is None or abs(width - self.last_width) > 0.0005:
                self.set_gripper(width)
        if down and "a3" in inputs:
            width = float(np.clip((inputs["a3"] + 1) / 2, 0, 1)) * GRIPPER_OPEN_M
            if self.last_width is None or abs(width - self.last_width) > 0.001:
                self.set_gripper(width)
        if "b2" in rising:
            self.set_gripper(GRIPPER_OPEN_M)
        if "b3" in rising:
            self.set_gripper(0.0)
        if "b8" in rising and not down and self.principal is not None:
            self.release()
            asyncio.get_running_loop().create_task(node.home_with(self, self.principal))
            return
        self.pressed_samples = self.pressed_samples + 1 if down else 0
        if not down:
            self.release()
            return
        axes = self.axes
        if axes is None:
            self.note = "Tap Align first" if self.frame == "align" else "Calibrate up, forward and left first"
            self.release()
            return
        if self.mode != "motion":
            if self.pressed_samples <= SETTLE_SAMPLES:
                return
            self.release()
            self.drive.begin()
            self.mode = "motion"
            self.reference = (position.copy(), rotation.copy(), self.drive.goal_position.copy(), self.drive.goal_rotation.copy())
        phone_position, phone_rotation, tip_position, tip_rotation = self.reference
        delta = axes @ (position - phone_position) * self.scale
        if np.linalg.norm(delta) > MAX_REACH_M:
            delta *= MAX_REACH_M / np.linalg.norm(delta)
        if self.use_rotation:
            turn = np.sign(np.linalg.det(axes)) * axes @ pin.log3(rotation @ phone_rotation.T)
            goal_rotation = pin.exp3(turn) @ tip_rotation
        else:
            goal_rotation = tip_rotation
        self.note = self.drive.track(tip_position + delta, goal_rotation, self.period)

    def status(self):
        sample = self.source.latest()
        return {
            "name": self.name, "active": self.active, "source": self.source.status, "mode": self.mode, "note": self.note,
            "age_ms": round((time.monotonic() - sample[0]) * 1000) if sample else None, "view": self.view, "rotation": self.use_rotation,
            "scale": self.scale, "ready": self.axes is not None, "calibrating": self.calibrating, "calibration_steps": sorted(self.calibration),
        }
