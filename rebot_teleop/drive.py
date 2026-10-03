import numpy as np
import pinocchio as pin


MIN_TIP_Z = 0.03
JUMP_RAD = 0.35


class CartesianDrive:
    def __init__(self, node, track_linear=1.0, track_angular=np.radians(360.0)):
        self.node = node
        self.track_linear = track_linear
        self.track_angular = track_angular
        self.active = False
        self.goal_position = None
        self.goal_rotation = None
        self.solution = None
        self.note = None

    def begin(self):
        node = self.node
        start = np.asarray(node.arm.command)[:6].copy()
        self.goal_position, self.goal_rotation = node.kin.pose(start)
        self.solution = start
        self.active = True
        self.note = None
        node.clutch = True
        node.arm.enabled = True

    def end(self):
        if self.active:
            self.node.arm.stop()
            self.node.clutch = False
        self.active = False

    def track(self, position, rotation, dt):
        if not self.active:
            self.begin()
        offset = np.asarray(position, dtype=float) - self.goal_position
        limit = self.track_linear * dt
        if np.linalg.norm(offset) > limit:
            offset *= limit / np.linalg.norm(offset)
        turn = pin.log3(np.asarray(rotation, dtype=float) @ self.goal_rotation.T)
        angle = np.linalg.norm(turn)
        if angle > self.track_angular * dt:
            turn *= self.track_angular * dt / angle
        return self.solve(self.goal_position + offset, pin.exp3(turn) @ self.goal_rotation)

    def solve(self, position, rotation):
        node = self.node
        kin = node.kin
        seed = np.clip(self.solution, kin.lower + 0.009, kin.upper - 0.009)
        q, residual = kin.inverse_pose(position, rotation, seed)
        if residual > 0.01:
            q, residual = kin.inverse_pose(position, rotation, seed, orientation_weight=0.03)
            if residual <= 0.01:
                rotation = kin.pose(q)[1]
        try:
            if residual > 0.01:
                raise ValueError("Out of reach")
            jump = np.max(np.abs(q - self.solution))
            if jump > JUMP_RAD:
                q = self.solution + (q - self.solution) * JUMP_RAD / jump
                position, rotation = kin.pose(q)
            if kin.pose(q)[0][2] < MIN_TIP_Z:
                raise ValueError("Too close to the table")
            kin.validate(q)
            node.arm.check_limits(q)
            node.arm.write(q, 0.02)
        except ValueError as error:
            self.note = str(error)
            return self.note
        self.solution = q
        self.goal_position = np.asarray(position, dtype=float)
        self.goal_rotation = np.asarray(rotation, dtype=float)
        self.note = None
        return None
