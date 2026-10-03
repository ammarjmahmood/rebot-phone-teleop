import math
from pathlib import Path
import numpy as np
import pinocchio as pin


class Kinematics:
    def __init__(self, urdf: Path):
        self.model = pin.buildModelFromUrdf(str(urdf))
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

    def validate(self, q, min_z=-0.05, max_radius=0.8):
        q = np.asarray(q, dtype=float)
        if q.shape != (6,) or not np.isfinite(q).all():
            raise ValueError("Six finite joint positions are required")
        outside = np.flatnonzero((q < self.lower) | (q > self.upper))
        if outside.size:
            joint = int(outside[0])
            raise ValueError(f"Joint {joint + 1} limit reached at {math.degrees(q[joint]):.1f} degrees")
        points = self.points(q)
        if np.any(points[:, 2] < min_z) or np.any(np.linalg.norm(points[:, :2], axis=1) > max_radius):
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
