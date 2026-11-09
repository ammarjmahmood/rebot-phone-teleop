import json
import math
import os
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
        p.setJointMotorControlArray(arm.body, arm.joints, p.VELOCITY_CONTROL, forces=[0.0] * 6)
        for joint, model in zip(arm.joints, arm.models):
            inertia = np.array(p.getDynamicsInfo(arm.body, joint)[2]) + ARMATURE[model]
            p.changeDynamics(arm.body, joint, linearDamping=0, angularDamping=0, jointDamping=0.02, localInertiaDiagonal=inertia.tolist())
        for link in (-1, arm.joints[0]):
            p.setCollisionFilterPair(arm.body, self.post, link, -1, 0)
        for joint, value in zip(arm.joints, arm.start):
            p.resetJointState(arm.body, joint, float(value))
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
            if tick % 50 == 0:
                self._check_contacts(p)
            if self.gui and tick % 250 == 0:
                self._labels(p)
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
