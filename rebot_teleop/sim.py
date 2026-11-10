import json
import math
import os
import pkgutil
import re
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np


ASSETS = Path(__file__).resolve().parents[1] / "assets"
MESH_DIRS = (
    os.environ.get("REBOT_SIM_MESHES", ""),
    str(ASSETS.parent / "meshes"),
    str(Path.home() / "rebot_isaac_sim/upstream/Rebot_Arm_description/RS/meshes"),
    str(Path.home() / "rebotprinterautomation/.research/rebot/Rebot_Arm_description/RS/meshes"),
)
PEAK_NM = {"rs-06": 36.0, "rs-00": 14.0}
CONTINUOUS_NM = {"rs-06": 11.0, "rs-00": 5.0}
ARMATURE = {"rs-06": 0.01, "rs-00": 0.002}
AMBIENT_C = 25.0
THERMAL_TAU_S = 600.0
BODY = ASSETS / "robot_body"
GRIPPER_OPEN_RAD = math.radians(270.0)
GRIPPER_RATE = math.radians(400.0)
FINGER_TRAVEL = (0.05, 0.0715)
CAMERA_SIZE = (320, 240)
CAMERA_FOV_DEG = 70.0
CAMERA_EVERY_S = 0.1
CAMERA_IDLE_S = 3.0
VIEW_SIZE = (640, 480)
VIEW_EVERY_S = 0.066


def camera_geometry(view, fov_vertical_deg, width, height, depth, near, far):
    gl_from_world = np.asarray(view, dtype=float).reshape(4, 4).T
    world_from_gl = np.linalg.inv(gl_from_world)
    world_from_camera = world_from_gl.copy()
    world_from_camera[:3, :3] = world_from_gl[:3, :3] @ np.diag([1.0, -1.0, -1.0])
    focal = (height / 2) / math.tan(math.radians(fov_vertical_deg) / 2)
    metres = far * near / (far - (far - near) * depth)
    return {"width": width, "height": height, "fx": focal, "fy": focal, "cx": width / 2, "cy": height / 2, "world_from_camera": world_from_camera, "depth_m": metres}


def find_meshes():
    for folder in MESH_DIRS:
        if folder and (Path(folder) / "shared" / "link1.STL").exists() and (Path(folder) / "visual" / "motor_4.STL").exists():
            return Path(folder)
    raise FileNotFoundError("reBot RS meshes not found; set REBOT_SIM_MESHES to the meshes folder that holds shared/ and visual/")


def sim_urdf():
    meshes = find_meshes()
    text = (ASSETS / "rebot_rs.urdf").read_text()
    text = re.sub(r'filename="\.\./meshes/([^"]+)"', lambda m: f'filename="{meshes / m.group(1)}"', text)
    text = text.replace('filename="camera/', f'filename="{ASSETS / "camera"}/')
    path = Path(tempfile.gettempdir()) / "rebot_rs_sim.urdf"
    path.write_text(text)
    return path


class SimMotor:
    def __init__(self, arm, index, motor_id, model):
        self.arm = arm
        self.index = index
        self.id = motor_id
        self.model = model

    def robstride_ping_host_id(self, host, timeout):
        return self.id, host

    def robstride_get_param_f32(self, param, timeout):
        with self.arm.world.lock:
            return float(self.arm.pos[self.index])

    def ensure_mode(self, mode, timeout):
        pass

    def send_mit(self, pos, vel, kp, kd, tau):
        with self.arm.world.lock:
            self.arm.mit[self.index] = (float(pos), float(vel), float(kp), float(kd), float(tau))
            self.arm.last_command = time.monotonic()

    def get_state(self):
        with self.arm.world.lock:
            arm = self.arm
            return SimpleNamespace(pos=float(arm.pos[self.index]), vel=float(arm.vel[self.index]), torq=float(arm.torque[self.index]), t_mos=float(arm.temperature[self.index]))


class SimArm:
    def __init__(self, world, channel, mount, start=None):
        self.world = world
        self.channel = channel
        self.mount = mount
        self.start = np.zeros(6) if start is None else np.asarray(start, dtype=float)
        self.body = None
        self.joints = []
        self.fingers = []
        self.pos = np.append(self.start, 0.0)
        self.vel = np.zeros(7)
        self.torque = np.zeros(7)
        self.temperature = np.full(7, AMBIENT_C)
        self.mit = [(0.0, 0.0, 0.0, 0.0, 0.0)] * 7
        self.enabled = False
        self.last_command = 0.0
        self.models = ["rs-06"] * 3 + ["rs-00"] * 4
        self.tip_link = None
        self.tip = (np.zeros(3), np.eye(3))
        self.camera_link = None
        self.camera_wanted = 0.0
        self.camera_at = 0.0
        self.frame = None

    def add_robstride_motor(self, motor_id, host, model):
        self.models[motor_id - 1] = model
        return SimMotor(self, motor_id - 1, motor_id, model)

    def enable_all(self):
        with self.world.lock:
            self.enabled = True

    def disable_all(self):
        with self.world.lock:
            self.enabled = False

    def close(self):
        self.disable_all()


class SimWorld:
    def __init__(self, gui=False, rate_hz=1000.0, heat=None):
        self.gui = gui
        self.dt = 1.0 / rate_hz
        self.heat = float(heat if heat is not None else os.environ.get("REBOT_SIM_HEAT", "1"))
        self.lock = threading.RLock()
        self.arms = {}
        self.pending = []
        self.contacts = []
        self.running = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.error = None
        self.time = 0.0
        self.view_params = None
        self.view_wanted = 0.0
        self.view_at = 0.0
        self.view = None
        self.scene_requests = []
        self.scene_bodies = []
        self.table_body = None
        self.objects = {}
        self.table = None

    def add_arm(self, channel, mount, start=None):
        with self.lock:
            arm = SimArm(self, channel, mount, start)
            self.arms[channel] = arm
            self.pending.append(arm)
        self.start()
        return arm

    def controller(self, channel):
        self.start()
        if not self.ready.wait(60):
            raise RuntimeError(self.error or "The simulator did not start")
        if channel not in self.arms:
            raise RuntimeError(f"No simulated arm on {channel}")
        return self.arms[channel]

    def start(self):
        if self.thread is None:
            self.running.set()
            self.thread = threading.Thread(target=self._run, name="pybullet", daemon=True)
            self.thread.start()

    def close(self):
        self.running.clear()
        if self.thread:
            self.thread.join(timeout=3)

    def _setup(self, p):
        self.client = p.connect(p.GUI if self.gui else p.DIRECT)
        self.renderer = p.ER_BULLET_HARDWARE_OPENGL if self.gui else p.ER_TINY_RENDERER
        self.render_note = "window" if self.gui else "cpu"
        if not self.gui and os.environ.get("REBOT_SIM_EGL", "1") == "1":
            try:
                egl = pkgutil.get_loader("eglRenderer")
                if egl and p.loadPlugin(egl.get_filename(), "_eglRendererPlugin") >= 0:
                    self.renderer = p.ER_BULLET_HARDWARE_OPENGL
                    self.render_note = "gpu"
            except Exception:
                pass
        p.setGravity(0, 0, -9.81)
        p.setTimeStep(self.dt)
        p.setPhysicsEngineParameter(numSolverIterations=50)
        if self.gui:
            p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0)
            p.resetDebugVisualizerCamera(2.0, 160, -12, [0.0, 0, 0.85])
        from rebot_teleop.node import LEG_TOP_M, LEG_W_M, LEG_D_M
        floor = p.createCollisionShape(p.GEOM_BOX, halfExtents=[1.5, 1.5, 0.01])
        p.createMultiBody(0, floor, p.createVisualShape(p.GEOM_BOX, halfExtents=[1.5, 1.5, 0.01], rgbaColor=[0.55, 0.36, 0.22, 1]), [0, 0, -0.01])
        half = [LEG_D_M / 2, LEG_W_M / 2, LEG_TOP_M / 2]
        self.post = p.createMultiBody(0, p.createCollisionShape(p.GEOM_BOX, halfExtents=half), p.createVisualShape(p.GEOM_BOX, halfExtents=half, rgbaColor=[0.96, 0.96, 0.95, 1]), [0, 0, half[2]])
        self.body_parts = [self.post]
        turn = p.getQuaternionFromEuler([0, 0, math.pi / 2])
        colors = json.loads((BODY / "colors.json").read_text()) if (BODY / "colors.json").exists() else {}
        for name, rgb in colors.items():
            mesh = BODY / f"{name}.stl"
            if mesh.exists():
                shape = p.createVisualShape(p.GEOM_MESH, fileName=str(mesh), meshScale=[0.001] * 3, rgbaColor=[*rgb, 1])
                self.body_parts.append(p.createMultiBody(0, -1, shape, [0, 0, LEG_TOP_M], turn))
        head = [0.035, 0.0924, 0.053]
        self.body_parts.append(p.createMultiBody(0, p.createCollisionShape(p.GEOM_BOX, halfExtents=head), -1, [0.0, 0.0, LEG_TOP_M + 0.0684]))
        collar = [0.035, 0.035, 0.031]
        self.body_parts.append(p.createMultiBody(0, p.createCollisionShape(p.GEOM_BOX, halfExtents=collar), -1, [0.0, 0.0, LEG_TOP_M - 0.014]))
        self.urdf = str(sim_urdf())

    def _load(self, p, arm):
        import pinocchio as pin
        quaternion = [float(v) for v in pin.Quaternion(arm.mount.rotation).coeffs()]
        arm.body = p.loadURDF(self.urdf, list(arm.mount.translation), quaternion, useFixedBase=True, flags=p.URDF_USE_INERTIA_FROM_FILE)
        names = {p.getJointInfo(arm.body, i)[1].decode(): i for i in range(p.getNumJoints(arm.body))}
        arm.joints = [names[f"joint{i}"] for i in range(1, 7)]
        arm.fingers = [names["gripper_joint1"], names["gripper_joint2"]]
        arm.tip_link = names["j_gripper_end"]
        arm.camera_link = names.get("j_wrist_camera")
        p.setJointMotorControlArray(arm.body, arm.joints, p.VELOCITY_CONTROL, forces=[0.0] * 6)
        for joint, model in zip(arm.joints, arm.models):
            inertia = np.array(p.getDynamicsInfo(arm.body, joint)[2]) + ARMATURE[model]
            p.changeDynamics(arm.body, joint, linearDamping=0, angularDamping=0, jointDamping=0.02, localInertiaDiagonal=inertia.tolist())
        for link in (-1, arm.joints[0]):
            p.setCollisionFilterPair(arm.body, self.post, link, -1, 0)
        for joint, value in zip(arm.joints, arm.start):
            p.resetJointState(arm.body, joint, float(value))
        for link in arm.fingers:
            p.changeDynamics(arm.body, link, lateralFriction=2.0, spinningFriction=0.01, frictionAnchor=1)
        arm.label = None

    def _run(self):
        try:
            import pybullet as p
            self._setup(p)
        except Exception as error:
            self.error = f"Simulator failed to start: {error}"
            self.ready.set()
            return
        next_tick = time.perf_counter()
        tick = 0
        while self.running.is_set():
            with self.lock:
                pending, self.pending = self.pending, []
            for arm in pending:
                try:
                    self._load(p, arm)
                except Exception as error:
                    self.error = f"Simulator could not load arm {arm.channel}: {error}"
                    with self.lock:
                        self.arms.pop(arm.channel, None)
            with self.lock:
                requests, self.scene_requests = self.scene_requests, []
            for spec, done in requests:
                try:
                    self._build_scene(p, spec)
                except Exception as error:
                    done.error = str(error)
                done.set()
            if pending:
                self.ready.set()
            elif not self.arms:
                self.ready.set()
            with self.lock:
                for arm in self.arms.values():
                    if arm.body is not None:
                        self._control(p, arm)
            p.stepSimulation()
            self.time += self.dt
            with self.lock:
                for arm in self.arms.values():
                    if arm.body is not None:
                        self._sense(p, arm)
            tick += 1
            if tick % 10 == 0:
                with self.lock:
                    for arm in self.arms.values():
                        if arm.body is not None:
                            self._track_tip(p, arm)
                    self._track_objects(p)
                self._apply_poses(p)
            if tick % 50 == 0:
                self._check_contacts(p)
            if self.gui and tick % 250 == 0:
                self._labels(p)
            if tick % 20 == 0:
                self._cameras(p)
            next_tick += self.dt
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:
                next_tick = time.perf_counter()
        p.disconnect()

    def _control(self, p, arm):
        if not arm.enabled:
            for i, joint in enumerate(arm.joints):
                p.resetJointState(arm.body, joint, arm.pos[i], 0.0)
            arm.torque[:] = 0
            return
        torques = []
        for i in range(6):
            pos, vel, kp, kd, tau = arm.mit[i]
            limit = PEAK_NM[arm.models[i]]
            torques.append(float(np.clip(kp * (pos - arm.pos[i]) + kd * (vel - arm.vel[i]) + tau, -limit, limit)))
        arm.torque[:6] = torques
        p.setJointMotorControlArray(arm.body, arm.joints, p.TORQUE_CONTROL, forces=torques)
        target = arm.mit[6][0]
        step = np.clip(target - arm.pos[6], -GRIPPER_RATE * self.dt, GRIPPER_RATE * self.dt)
        arm.pos[6] += step
        arm.vel[6] = step / self.dt
        arm.torque[6] = 0.2 if abs(target - arm.pos[6]) > 1e-3 else 0.05
        fraction = float(np.clip(arm.pos[6] / GRIPPER_OPEN_RAD, 0, 1))
        for joint, travel in zip(arm.fingers, FINGER_TRAVEL):
            p.setJointMotorControl2(arm.body, joint, p.POSITION_CONTROL, targetPosition=fraction * travel, force=40)

    def _sense(self, p, arm):
        self._heat(arm)
        if not arm.enabled:
            for i, joint in enumerate(arm.joints):
                p.resetJointState(arm.body, joint, arm.pos[i], 0.0)
            arm.vel[:6] = 0
            return
        states = p.getJointStates(arm.body, arm.joints)
        arm.pos[:6] = [s[0] for s in states]
        arm.vel[:6] = [s[1] for s in states]

    def _track_tip(self, p, arm):
        state = p.getLinkState(arm.body, arm.tip_link, computeForwardKinematics=True)
        arm.tip = (np.array(state[4]), np.array(p.getMatrixFromQuaternion(state[5])).reshape(3, 3))

    def _heat(self, arm):
        for i in range(7):
            gain = (100.0 - AMBIENT_C) / (CONTINUOUS_NM[arm.models[i]] ** 2 * THERMAL_TAU_S)
            change = gain * arm.torque[i] ** 2 - (arm.temperature[i] - AMBIENT_C) / THERMAL_TAU_S
            arm.temperature[i] += change * self.dt * self.heat

    def _check_contacts(self, p):
        found = []
        bodies = [arm for arm in self.arms.values() if arm.body is not None]
        for arm in bodies:
            for contact in p.getContactPoints(bodyA=arm.body):
                other = contact[2]
                if other == arm.body:
                    continue
                if other == self.table_body:
                    found.append((arm.channel, contact[3], "table"))
                    continue
                if other in self.scene_bodies:
                    continue
                what = "post" if other == self.post else "head" if other in self.body_parts else next((f"arm {o.channel}" for o in bodies if o.body == other), "floor")
                found.append((arm.channel, contact[3], what))
        with self.lock:
            self.contacts = sorted(set(found))

    def _labels(self, p):
        for arm in self.arms.values():
            if arm.body is None:
                continue
            text = arm.channel + "  " + " ".join(f"{t:.0f}" for t in arm.temperature[:6]) + " C"
            position = list(arm.mount.translation + np.array([0, 0, 0.12]))
            color = [0.8, 0.2, 0.1] if arm.temperature.max() >= 75 else [0.1, 0.1, 0.1]
            if arm.label is None:
                arm.label = p.addUserDebugText(text, position, color, 1.1)
            else:
                p.addUserDebugText(text, position, color, 1.1, replaceItemUniqueId=arm.label)

    def _cameras(self, p):
        now = time.monotonic()
        self._overview(p, now)
        for arm in list(self.arms.values()):
            if arm.body is None or arm.camera_link is None or now - arm.camera_wanted > CAMERA_IDLE_S or now - arm.camera_at < CAMERA_EVERY_S:
                continue
            state = p.getLinkState(arm.body, arm.camera_link, computeForwardKinematics=True)
            rotation = np.array(p.getMatrixFromQuaternion(state[5])).reshape(3, 3)
            eye = np.array(state[4]) + rotation[:, 2] * 0.021
            view = p.computeViewMatrix(eye.tolist(), (eye + rotation[:, 2]).tolist(), (-rotation[:, 0]).tolist())
            width, height = CAMERA_SIZE
            projection = p.computeProjectionMatrixFOV(CAMERA_FOV_DEG * height / width, width / height, 0.01, 4.0)
            renderer = self.renderer
            image = p.getCameraImage(width, height, view, projection, renderer=renderer)
            pixels = np.reshape(np.asarray(image[2], dtype=np.uint8), (height, width, 4))[:, :, :3].copy()
            geometry = camera_geometry(view, CAMERA_FOV_DEG * height / width, width, height, np.reshape(np.asarray(image[3], dtype=np.float32), (height, width)), 0.01, 4.0)
            with self.lock:
                arm.frame = (now, pixels)
                arm.geometry = geometry
                arm.camera_at = now

    def _overview(self, p, now):
        if self.view_params is None or now - self.view_wanted > CAMERA_IDLE_S or now - self.view_at < VIEW_EVERY_S:
            return
        params = self.view_params
        yaw, pitch, distance, height = params
        view = p.computeViewMatrixFromYawPitchRoll([0.0, 0.0, height], distance, yaw, pitch, 0.0, 2)
        width, tall = VIEW_SIZE
        projection = p.computeProjectionMatrixFOV(50.0, width / tall, 0.05, 10.0)
        renderer = self.renderer
        image = p.getCameraImage(width, tall, view, projection, renderer=renderer)
        pixels = np.reshape(np.asarray(image[2], dtype=np.uint8), (tall, width, 4))[:, :, :3].copy()
        geometry = camera_geometry(view, 50.0, width, tall, np.reshape(np.asarray(image[3], dtype=np.float32), (tall, width)), 0.05, 10.0)
        with self.lock:
            self.view = (now, params, pixels)
            self.view_geometry = geometry
            self.view_at = now

    def overview_frame(self, yaw, pitch, distance, height):
        params = (float(np.clip(yaw, -720, 720)), float(np.clip(pitch, -89, 89)), float(np.clip(distance, 0.3, 5.0)), float(np.clip(height, 0.0, 2.0)))
        with self.lock:
            self.view_params = params
            self.view_wanted = time.monotonic()
            if self.view and self.view[1] == params:
                return self.view[0], self.view[2]
            return None

    def set_scene(self, spec, timeout=10.0):
        self.start()
        done = threading.Event()
        done.error = None
        with self.lock:
            self.scene_requests.append((spec, done))
        if not done.wait(timeout):
            raise RuntimeError("The simulator did not build the scene")
        if done.error:
            raise ValueError(done.error)
        return self.object_states()

    def _build_scene(self, p, spec):
        for body in self.scene_bodies:
            if self.gui:
                self.parked = getattr(self, "parked", 0) + 1
                p.resetBasePositionAndOrientation(body, [50.0 + self.parked, 50.0, -5.0], [0, 0, 0, 1])
                p.setCollisionFilterGroupMask(body, -1, 0, 0)
            else:
                p.removeBody(body)
        self.scene_bodies = []
        self.table_body = None
        objects = {}
        table = spec.get("table")
        if table:
            half = [(table["x"][1] - table["x"][0]) / 2, (table["y"][1] - table["y"][0]) / 2, table["top"] / 2]
            center = [(table["x"][0] + table["x"][1]) / 2, (table["y"][0] + table["y"][1]) / 2, table["top"] / 2]
            body = p.createMultiBody(0, p.createCollisionShape(p.GEOM_BOX, halfExtents=half), p.createVisualShape(p.GEOM_BOX, halfExtents=half, rgbaColor=[0.78, 0.66, 0.5, 1]), center)
            p.changeDynamics(body, -1, lateralFriction=0.8)
            self.scene_bodies.append(body)
            self.table_body = body
        for item in spec.get("objects", []):
            color = list(item.get("rgba", [0.8, 0.1, 0.1, 1.0]))
            orientation = p.getQuaternionFromEuler([0.0, 0.0, math.radians(item.get("yaw_deg", 0.0))])
            if item["kind"] == "block":
                half = [item["size"] / 2] * 3
                body = p.createMultiBody(item.get("mass", 0.04), p.createCollisionShape(p.GEOM_BOX, halfExtents=half), p.createVisualShape(p.GEOM_BOX, halfExtents=half, rgbaColor=color), item["xyz"], orientation)
            elif item["kind"] == "bin":
                inner, wall, height = item["size"], item.get("wall", 0.006), item.get("height", 0.05)
                outer = inner / 2 + wall
                parts = [([outer, outer, wall / 2], [0, 0, wall / 2])]
                for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    extent = [wall / 2, outer, height / 2] if sx else [outer, wall / 2, height / 2]
                    parts.append((extent, [sx * (inner / 2 + wall / 2), sy * (inner / 2 + wall / 2), height / 2]))
                shapes = [p.GEOM_BOX] * len(parts)
                collision = p.createCollisionShapeArray(shapes, halfExtents=[e for e, _ in parts], collisionFramePositions=[c for _, c in parts])
                visual = p.createVisualShapeArray(shapes, halfExtents=[e for e, _ in parts], visualFramePositions=[c for _, c in parts], rgbaColors=[color] * len(parts))
                body = p.createMultiBody(item.get("mass", 0.3), collision, visual, item["xyz"], orientation)
            else:
                raise ValueError(f"Unknown object kind {item['kind']}")
            p.changeDynamics(body, -1, lateralFriction=1.2, spinningFriction=0.005, rollingFriction=0.0005)
            self.scene_bodies.append(body)
            objects[item["name"]] = {"body": body, "spec": dict(item)}
        with self.lock:
            self.objects = objects
            self.table = dict(table) if table else None
            self.scene_spec = spec
        self._track_objects(p)

    def _track_objects(self, p):
        if not self.objects:
            return
        poses = {}
        for name, item in self.objects.items():
            position, orientation = p.getBasePositionAndOrientation(item["body"])
            poses[name] = (np.array(position), math.degrees(p.getEulerFromQuaternion(orientation)[2]), np.array(p.getMatrixFromQuaternion(orientation)).reshape(3, 3), list(orientation))
        with self.lock:
            for name, pose in poses.items():
                if name in self.objects:
                    self.objects[name]["pose"] = pose

    def scene_snapshot(self):
        with self.lock:
            poses = {name: [[float(v) for v in item["pose"][0]], item["pose"][3]] for name, item in self.objects.items() if "pose" in item}
            return {"spec": getattr(self, "scene_spec", {}), "poses": poses}

    def set_object_poses(self, poses):
        with self.lock:
            self.pose_requests = dict(poses)

    def _apply_poses(self, p):
        with self.lock:
            poses, self.pose_requests = getattr(self, "pose_requests", None), None
        if not poses:
            return
        for name, (position, orientation) in poses.items():
            item = self.objects.get(name)
            if item:
                p.resetBasePositionAndOrientation(item["body"], position, orientation)

    def object_states(self):
        with self.lock:
            result = {}
            for name, item in self.objects.items():
                spec = item["spec"]
                position, yaw, rotation = item.get("pose", (np.array(spec["xyz"]), spec.get("yaw_deg", 0.0), np.eye(3), None))[:3]
                result[name] = {"kind": spec["kind"], "color": spec.get("color", ""), "size_m": spec["size"], "xyz": [round(float(v), 4) for v in position], "yaw_deg": round(float(yaw), 1), "upright": bool(rotation[2, 2] > 0.9)}
            return {"table": self.table, "objects": result}

    def camera_geometry(self, channel):
        with self.lock:
            if channel == "overview":
                return getattr(self, "view_geometry", None)
            return getattr(self.arms[channel], "geometry", None)

    def camera_capture(self, channel):
        with self.lock:
            if channel == "overview":
                if not self.view:
                    return None
                return self.view[0], self.view[2], getattr(self, "view_geometry", None)
            arm = self.arms[channel]
            arm.camera_wanted = time.monotonic()
            if not arm.frame:
                return None
            return arm.frame[0], arm.frame[1], getattr(arm, "geometry", None)

    def camera_frame(self, channel):
        with self.lock:
            arm = self.arms[channel]
            arm.camera_wanted = time.monotonic()
            return arm.frame

    def set_hand(self, channel, joints=None, gripper=None):
        with self.lock:
            arm = self.arms[channel]
            if arm.enabled:
                raise ValueError("The simulated motors are on; release torque before moving the arm by hand")
            if joints is not None:
                values = np.asarray(joints, dtype=float)
                if values.shape != (6,) or not np.isfinite(values).all():
                    raise ValueError("Six finite joint angles are required")
                arm.pos[:6] = values
            if gripper is not None:
                arm.pos[6] = float(gripper)
                arm.mit[6] = (float(gripper), 0.0, 0.0, 0.0, 0.0)

    def snapshot(self):
        with self.lock:
            return {
                "time_s": round(self.time, 2), "error": self.error,
                "contacts": [{"arm": a, "link": l, "with": w} for a, l, w in self.contacts],
                "arms": {channel: {"tip_xyz": [round(float(v), 4) for v in arm.tip[0]], "tip_rotation": arm.tip[1].round(4).tolist(), "temperatures": [round(float(t), 1) for t in arm.temperature], "torque_nm": [round(float(t), 2) for t in arm.torque]} for channel, arm in self.arms.items()},
            }

    def set_temperature(self, channel, joint, value):
        with self.lock:
            self.arms[channel].temperature[joint] = float(value)
