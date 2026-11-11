import math
import xml.etree.ElementTree as ET
import numpy as np
import pinocchio as pin
from rebot_teleop import config as C
from rebot_teleop.arm import LOWER, UPPER


def placement(xyz=(0.0, 0.0, 0.0), rpy_deg=(0.0, 0.0, 0.0)):
    return pin.SE3(pin.rpy.rpyToMatrix(*np.radians(np.asarray(rpy_deg, dtype=float))), np.asarray(xyz, dtype=float))


def visuals(urdf=C.URDF):
    root = ET.parse(urdf).getroot()
    colors = {}
    for material in root.findall("material"):
        color = material.find("color")
        if color is not None:
            colors[material.get("name")] = [float(v) for v in color.get("rgba").split()]
    out = []
    for link in root.findall("link"):
        for visual in link.findall("visual"):
            mesh = visual.find("geometry/mesh")
            if mesh is None:
                continue
            origin = visual.find("origin")
            xyz = [float(v) for v in (origin.get("xyz", "0 0 0") if origin is not None else "0 0 0").split()]
            rpy = [float(v) for v in (origin.get("rpy", "0 0 0") if origin is not None else "0 0 0").split()]
            scale = [float(v) for v in mesh.get("scale", "1 1 1").split()]
            material = visual.find("material")
            rgba = None
            if material is not None:
                inline = material.find("color")
                rgba = [float(v) for v in inline.get("rgba").split()] if inline is not None else colors.get(material.get("name"))
            out.append({"link": link.get("name"), "file": mesh.get("filename"), "xyz": xyz, "rpy": rpy, "scale": scale, "rgba": rgba or [0.7, 0.7, 0.7, 1.0]})
    return out


class Kinematics:
    def __init__(self, mount=None, urdf=C.URDF, counterbalance=None):
        self.model = pin.buildModelFromUrdf(str(urdf))
        self.counterbalance = counterbalance or None
        self.mount = placement(**mount) if mount else pin.SE3.Identity()
        root = self.model.getJointId("joint1")
        self.model.jointPlacements[root] = self.mount * self.model.jointPlacements[root]
        for index in range(len(self.model.frames)):
            frame = self.model.frames[index]
            if frame.parentJoint == 0 and frame.name != "universe":
                frame.placement = self.mount * frame.placement
                self.model.frames[index] = frame
        self.data = self.model.createData()
        self.indices = [self.model.joints[self.model.getJointId(f"joint{i}")].idx_q for i in range(1, 7)]
        self.lower = LOWER.copy()
        self.upper = UPPER.copy()
        self.tip = self.model.getFrameId("gripper_end")
        self.camera = self.model.getFrameId("wrist_camera") if self.model.existFrame("wrist_camera") else None
        self.link_frames = {}
        for frame in self.model.frames:
            if frame.type == pin.FrameType.BODY:
                self.link_frames[frame.name] = self.model.getFrameId(frame.name)
        self.finger = {}
        for name in ("gripper_joint1", "gripper_joint2"):
            if self.model.existJointName(name):
                joint = self.model.joints[self.model.getJointId(name)]
                self.finger[name] = joint.idx_q

    def configuration(self, q, width=None):
        full = pin.neutral(self.model)
        full[self.indices] = np.asarray(q, dtype=float)[:6]
        if width is not None:
            if "gripper_joint1" in self.finger:
                full[self.finger["gripper_joint1"]] = float(np.clip(width, 0, 0.05))
            if "gripper_joint2" in self.finger:
                full[self.finger["gripper_joint2"]] = float(np.clip(width, 0, 0.05))
        return full

    def pose(self, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        frame = data.oMf[self.tip]
        return frame.translation.copy(), frame.rotation.copy()

    def camera_pose(self, q):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q))
        frame = data.oMf[self.camera if self.camera is not None else self.tip]
        return frame.translation.copy(), frame.rotation.copy()

    def links(self, q, width=None):
        data = self.model.createData()
        pin.framesForwardKinematics(self.model, data, self.configuration(q, width))
        out = {}
        for name, fid in self.link_frames.items():
            m = data.oMf[fid]
            out[name] = np.vstack([np.hstack([m.rotation, m.translation[:, None]]), [0, 0, 0, 1]])
        return out

    def ik(self, position, rotation, seed, iterations=40, orientation_weight=0.5):
        target = pin.SE3(np.asarray(rotation, dtype=float), np.asarray(position, dtype=float))
        best = None
        for weight in (orientation_weight, 0.05):
            q, err_p, err_r = self._solve(target, seed, iterations, weight)
            if best is None or err_p < best[1] - 1e-4:
                best = (q, err_p, err_r)
            if err_p < 0.004:
                break
        return best

    def _solve(self, target, seed, iterations, weight):
        model = self.model
        data = model.createData()
        q = self.configuration(np.clip(seed, self.lower, self.upper))
        weights = np.array([1.0, 1.0, 1.0, weight, weight, weight])
        for _ in range(iterations):
            pin.framesForwardKinematics(model, data, q)
            current = data.oMf[self.tip]
            error = np.concatenate([target.translation - current.translation, pin.log3(target.rotation @ current.rotation.T)])
            if np.linalg.norm(error[:3]) < 5e-4 and np.linalg.norm(error[3:]) * weight < 5e-3:
                break
            pin.computeJointJacobians(model, data, q)
            jacobian = pin.getFrameJacobian(model, data, self.tip, pin.ReferenceFrame.LOCAL_WORLD_ALIGNED)[:, self.indices]
            weighted = jacobian * weights[:, None]
            damping = 1e-3 + 0.02 * float(np.linalg.norm(error[:3]))
            delta = weighted.T @ np.linalg.solve(weighted @ weighted.T + np.eye(6) * damping, error * weights)
            q[self.indices] = np.clip(q[self.indices] + np.clip(delta, -0.15, 0.15), self.lower, self.upper)
        pin.framesForwardKinematics(model, data, q)
        current = data.oMf[self.tip]
        return q[self.indices].copy(), float(np.linalg.norm(target.translation - current.translation)), float(np.linalg.norm(pin.log3(target.rotation @ current.rotation.T)))

    def gravity_function(self):
        model = self.model
        data = model.createData()
        indices = list(self.indices)

        counter = self.counterbalance

        def gravity(q):
            full = pin.neutral(model)
            full[indices] = np.asarray(q, dtype=float)[:6]
            torque = pin.computeGeneralizedGravity(model, data, full)[indices].copy()
            if counter:
                torque[0] -= assist(counter, q[0])
            return torque

        return gravity


def assist(counter, q1):
    forward = float(counter.get("forward", 1.0))
    swing = forward * (float(q1) - math.radians(float(counter.get("zero_deg", 0.0))))
    swing = min(max(swing, 0.0), math.radians(float(counter.get("max_deg", 170.0))))
    return forward * float(counter.get("nm_per_rad", 0.0)) * swing


def top_down(yaw_deg=0.0):
    yaw = math.radians(yaw_deg)
    down = np.array([0.0, 0.0, -1.0])
    across = np.array([math.cos(yaw), math.sin(yaw), 0.0])
    third = np.cross(down, across)
    return np.column_stack([down, across, third])
