import math
import time
from pathlib import Path
import numpy as np
import pinocchio as pin


def mount_placement(xyz=(0.0, 0.0, 0.0), rpy_deg=(0.0, 0.0, 0.0)):
    return pin.SE3(pin.rpy.rpyToMatrix(*np.radians(np.asarray(rpy_deg, dtype=float))), np.asarray(xyz, dtype=float))


class Kinematics:
    def __init__(self, urdf: Path, mount=None):
        self.model = pin.buildModelFromUrdf(str(urdf))
        self.mount = mount if mount is not None else pin.SE3.Identity()
        root = self.model.getJointId("joint1")
        self.model.jointPlacements[root] = self.mount * self.model.jointPlacements[root]
        for index in range(len(self.model.frames)):
            frame = self.model.frames[index]
            if frame.parentJoint == 0 and frame.name != "universe":
                frame.placement = self.mount * frame.placement
                self.model.frames[index] = frame
        self.data = self.model.createData()
        self.indices = [self.model.joints[self.model.getJointId(f"joint{i}")].idx_q for i in range(1, 7)]
        self.lower = self.model.lowerPositionLimit[self.indices].copy()
        self.upper = self.model.upperPositionLimit[self.indices].copy()
        names = [frame.name for frame in self.model.frames]
        tip = next((name for name in ("gripper_end", "end_link") if name in names), None)
        if tip is None:
            raise ValueError("Missing end effector frame")
        self.tip = self.model.getFrameId(tip)
        self.keep_out = []
        self.clearance = 0.035

    def clear_of(self, points, strict=False):
        if not self.keep_out:
            return None
        chain = points[3:]
        samples = [chain[0]]
        fingers = []
        for index, (a, b) in enumerate(zip(chain[:-1], chain[1:])):
            last = index == len(chain) - 2
            for u in (0.25, 0.5, 0.75, 1.0):
                (fingers if last and u >= 0.5 else samples).append(a + (b - a) * u)
        for box in self.keep_out:
            name, center, half = box[:3]
            near = box[3] if len(box) > 3 and not strict else self.clearance
            gap = np.maximum(np.abs(np.asarray(samples) - center) - half, 0.0)
            if np.min(np.linalg.norm(gap, axis=1)) < self.clearance:
                return name
            gap = np.maximum(np.abs(np.asarray(fingers) - center) - half, 0.0)
            if np.min(np.linalg.norm(gap, axis=1)) < near:
                return name
        return None

    def configuration(self, q):
        full = pin.neutral(self.model)
        full[self.indices] = np.asarray(q, dtype=float)
        return full

    def pose(self, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        frame = data.oMf[self.tip]
        return frame.translation.copy(), frame.rotation.copy()

    def frame_pose(self, name, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        frame = data.oMf[self.model.getFrameId(name)]
        return frame.translation.copy(), frame.rotation.copy()

    def points(self, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        points = [self.mount.translation.copy()]
        for i in range(1, 7):
            points.append(data.oMi[self.model.getJointId(f"joint{i}")].translation.copy())
        points.append(data.oMf[self.tip].translation.copy())
        return np.array(points)

    def validate(self, q, min_z=-0.05, max_radius=0.8, strict=False):
        q = np.asarray(q, dtype=float)
        if q.shape != (6,) or not np.isfinite(q).all():
            raise ValueError("Six finite joint positions are required")
        outside = np.flatnonzero((q < self.lower) | (q > self.upper))
        if outside.size:
            joint = int(outside[0])
            raise ValueError(f"Joint {joint + 1} limit reached at {math.degrees(q[joint]):.1f} degrees")
        points = self.points(q)
        local = (points - self.mount.translation) @ self.mount.rotation
        if np.any(points[:, 2] < min_z) or np.any(local[:, 2] < min_z):
            raise ValueError("Workspace boundary reached")
        if np.any(np.linalg.norm(local[:, :2], axis=1) > max_radius):
            raise ValueError("Workspace boundary reached")
        hit = self.clear_of(points, strict)
        if hit:
            raise ValueError(f"The arm would hit the {hit}")

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

    def segment_clear(self, start, goal, step_deg=2.0, strict=(1.0, 0.0)):
        start = np.asarray(start, dtype=float)
        goal = np.asarray(goal, dtype=float)
        count = max(4, int(math.ceil(np.max(np.abs(goal - start)) / math.radians(step_deg))))
        for u in np.linspace(0.0, 1.0, count + 1)[1:]:
            try:
                self.validate(start + (goal - start) * u, strict=strict[0] <= u <= strict[1])
            except ValueError as error:
                return str(error)
        return None

    def route(self, start, goal, budget_s=1.5, seed=0):
        start = np.asarray(start, dtype=float)
        goal = np.asarray(goal, dtype=float)
        self.validate(goal)
        blocked = self.segment_clear(start, goal)
        if blocked is None:
            return [goal]
        rng = np.random.default_rng(seed)
        middle = (start + goal) / 2
        spread = np.maximum(np.abs(goal - start), math.radians(30.0))
        best = None
        deadline = time.monotonic() + budget_s
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            scale = min(2.0, 0.5 + attempt / 200)
            mid = np.clip(middle + rng.normal(0.0, 1.0, 6) * spread * scale, self.lower + 0.02, self.upper - 0.02)
            length = np.max(np.abs(mid - start)) + np.max(np.abs(goal - mid))
            if best is not None and length >= best[0]:
                continue
            first = (0.3, 1.0)
            second = (0.0, 0.7)
            if self.segment_clear(start, mid, 8.0, first) or self.segment_clear(mid, goal, 8.0, second):
                continue
            if self.segment_clear(start, mid, 2.0, first) is None and self.segment_clear(mid, goal, 2.0, second) is None:
                best = (length, mid)
                if attempt > 60:
                    break
        if best is None:
            raise ValueError(f"No collision free path found; the straight path fails with: {blocked}")
        return [best[1], goal]

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
