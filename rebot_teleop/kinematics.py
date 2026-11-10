import math
from pathlib import Path
import numpy as np
import pinocchio as pin


GRAVITY_MS2 = 9.81
DEFAULT_FLOOR_M = 0.05
MOUNT_PRESETS = {
    "upright": (0.0, 0.0, 0.0),
    "inverted": (180.0, 0.0, 0.0),
    "side_left": (-90.0, 0.0, 0.0),
    "side_right": (90.0, 0.0, 0.0),
}


def rpy_matrix(roll, pitch, yaw):
    roll, pitch, yaw = (math.radians(float(v)) for v in (roll, pitch, yaw))
    rx = np.array([[1.0, 0.0, 0.0], [0.0, math.cos(roll), -math.sin(roll)], [0.0, math.sin(roll), math.cos(roll)]])
    ry = np.array([[math.cos(pitch), 0.0, math.sin(pitch)], [0.0, 1.0, 0.0], [-math.sin(pitch), 0.0, math.cos(pitch)]])
    rz = np.array([[math.cos(yaw), -math.sin(yaw), 0.0], [math.sin(yaw), math.cos(yaw), 0.0], [0.0, 0.0, 1.0]])
    return rz @ ry @ rx


def parse_mount(spec):
    if spec is None or not str(spec).strip():
        return np.eye(3), DEFAULT_FLOOR_M
    body, _, height = str(spec).strip().partition("@")
    body = body.strip()
    floor = DEFAULT_FLOOR_M
    if height.strip():
        floor = float(height)
        if not math.isfinite(floor) or floor < 0:
            raise ValueError("Mount height must be a distance in metres, for example side_left@1.15")
    if body in MOUNT_PRESETS:
        return rpy_matrix(*MOUNT_PRESETS[body]), floor
    parts = [p for p in body.replace(" ", "").split(",") if p]
    if len(parts) != 3:
        raise ValueError("Mount must be " + ", ".join(sorted(MOUNT_PRESETS)) + " or three angles in degrees such as -90,0,0")
    return rpy_matrix(*(float(p) for p in parts)), floor


class Kinematics:
    def __init__(self, urdf: Path, mount=None, floor_m=DEFAULT_FLOOR_M):
        self.model = pin.buildModelFromUrdf(str(urdf))
        self.mount = np.eye(3) if mount is None else np.asarray(mount, dtype=float).reshape(3, 3)
        self.floor_m = float(floor_m)
        self.model.gravity.linear = self.mount.T @ np.array([0.0, 0.0, -GRAVITY_MS2])
        self.data = self.model.createData()
        self.indices = [self.model.joints[self.model.getJointId(f"joint{i}")].idx_q for i in range(1, 7)]
        self.lower = self.model.lowerPositionLimit[self.indices].copy()
        self.upper = self.model.upperPositionLimit[self.indices].copy()
        names = [frame.name for frame in self.model.frames]
        tip = next((name for name in ("gripper_end", "end_link") if name in names), None)
        if tip is None:
            raise ValueError("Missing end effector frame")
        self.tip = self.model.getFrameId(tip)

    def configuration(self, q):
        full = pin.neutral(self.model)
        full[self.indices] = np.asarray(q, dtype=float)
        return full

    def pose(self, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        frame = data.oMf[self.tip]
        return frame.translation.copy(), frame.rotation.copy()

    def points(self, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        points = [np.zeros(3)]
        for i in range(1, 7):
            points.append(data.oMi[self.model.getJointId(f"joint{i}")].translation.copy())
        points.append(data.oMf[self.tip].translation.copy())
        return np.array(points)

    def world_points(self, q):
        return self.points(q) @ self.mount.T

    def validate(self, q, min_z=None, max_radius=0.8):
        q = np.asarray(q, dtype=float)
        if q.shape != (6,) or not np.isfinite(q).all():
            raise ValueError("Six finite joint positions are required")
        outside = np.flatnonzero((q < self.lower) | (q > self.upper))
        if outside.size:
            joint = int(outside[0])
            raise ValueError(f"Joint {joint + 1} limit reached at {math.degrees(q[joint]):.1f} degrees")
        points = self.world_points(q)
        floor = -self.floor_m if min_z is None else min_z
        if np.any(points[:, 2] < floor) or np.any(np.linalg.norm(points[:, :2], axis=1) > max_radius):
            raise ValueError("Workspace boundary reached")

    def inverse_pose(self, position, rotation, seed, orientation_weight=0.4, iterations=60):
        data = self.model.createData()
        target = pin.SE3(np.asarray(rotation, dtype=float), np.asarray(position, dtype=float))
        q = self.configuration(seed)
        weights = np.array([1.0, 1.0, 1.0, orientation_weight, orientation_weight, orientation_weight])
        for _ in range(iterations):
            pin.framesForwardKinematics(self.model, data, q)
            current = data.oMf[self.tip]
            error = np.concatenate([target.translation - current.translation, pin.log3(target.rotation @ current.rotation.T)])
            if np.linalg.norm(error[:3]) < 0.001 and np.linalg.norm(error[3:]) < 0.01:
                break
            pin.computeJointJacobians(self.model, data, q)
            jacobian = pin.getFrameJacobian(self.model, data, self.tip, pin.ReferenceFrame.LOCAL_WORLD_ALIGNED)[:, self.indices]
            weighted = jacobian * weights[:, None]
            delta = weighted.T @ np.linalg.solve(weighted @ weighted.T + np.eye(6) * 0.002, error * weights)
            q[self.indices] = np.clip(q[self.indices] + np.clip(delta, -0.1, 0.1), self.lower + 0.009, self.upper - 0.009)
        pin.framesForwardKinematics(self.model, data, q)
        residual = float(np.linalg.norm(target.translation - data.oMf[self.tip].translation))
        return q[self.indices].copy(), residual

    def trajectory(self, start, goal, speed, acceleration, dt=0.02):
        start = np.asarray(start, dtype=float)
        delta = np.asarray(goal, dtype=float) - start
        distance = float(np.max(np.abs(delta)))
        duration = max(0.2, 1.875 * distance / speed, math.sqrt(5.774 * distance / acceleration))
        count = math.ceil(duration / dt)
        samples = []
        for t in np.linspace(0, duration, count + 1):
            u = t / duration
            q = start + delta * (10 * u**3 - 15 * u**4 + 6 * u**5)
            self.validate(q)
            samples.append((float(t), q))
        return samples

    def gravity_function(self):
        model = self.model
        data = model.createData()
        indices = list(self.indices)

        def gravity(q):
            full = pin.neutral(model)
            full[indices] = np.asarray(q, dtype=float)
            return pin.computeGeneralizedGravity(model, data, full)[indices].copy()

        return gravity
