import math
import threading
import time
from pathlib import Path
import numpy as np
from rebot_teleop import config as C
from rebot_teleop.arm import Arm, LOWER, UPPER, min_jerk
from rebot_teleop import heat as H
from rebot_teleop.kin import Kinematics, top_down

NET = Path("/sys/class/net")


def can_interfaces():
    found = {}
    for path in sorted(NET.iterdir()) if NET.exists() else []:
        try:
            if (path / "type").read_text().strip() != "280":
                continue
            port = (path / "device").resolve().name.split(":")[0] if (path / "device").exists() else None
            state = (path / "operstate").read_text().strip()
        except OSError:
            continue
        found[path.name] = {"usb": port, "state": state}
    return found


def resolve_channel(name, ports, fallback):
    port = ports.get(name)
    interfaces = can_interfaces()
    if port:
        for iface, info in interfaces.items():
            if info["usb"] == port:
                return iface, info["state"]
        return None, f"the CAN adapter on USB port {port} is unplugged"
    if fallback in interfaces:
        return fallback, interfaces[fallback]["state"]
    return None, f"{fallback} is not present"


class Unit:
    def __init__(self, name, robot, factory=None):
        self.name = name
        self.robot = robot
        settings = C.load(f"arms/{name}/settings.json", {})
        self.mount = settings.get("mount") or C.MOUNTS.get(name) or {"xyz": [0, 0, 0], "rpy_deg": [0, 0, 0]}
        self.counterbalance = settings.get("counterbalance")
        self.kin = Kinematics(self.mount, counterbalance=self.counterbalance)
        self.factory = factory
        self.channel = C.DEFAULT_CAN.get(name, "can0")
        self.arm = Arm(name, self.channel, gravity=self.kin.gravity_function(), controller_factory=factory, motors=settings.get("motors"))
        self.arm.home_q = np.radians(C.POSES_DEG["home"][name])
        self.arm.relief = self.relief_path
        self.load_lock = threading.Lock()
        self.load_gravity = self.kin.gravity_function()
        if self.counterbalance and self.counterbalance.get("j2_min_deg") is not None:
            floor = math.radians(float(self.counterbalance["j2_min_deg"]))
            self.arm.lower[1] = max(self.arm.lower[1], floor)
            self.kin.lower[1] = max(self.kin.lower[1], floor)
        gripper = settings.get("gripper_range")
        if gripper:
            try:
                self.arm.set_gripper_range(math.radians(gripper["closed"]), math.radians(gripper["open"]))
            except (KeyError, ValueError):
                pass
        self.note = None
        self.thread = threading.Thread(target=self._watch, name=f"watch-{name}", daemon=True)

    def _watch(self):
        last_try = 0.0
        while not self.robot.closing.is_set():
            arm = self.arm
            if not arm.connected:
                if time.monotonic() - last_try > 2.0:
                    last_try = time.monotonic()
                    if self.factory is None:
                        iface, state = resolve_channel(self.name, self.robot.ports, C.DEFAULT_CAN.get(self.name, "can0"))
                        if iface is None:
                            self.note = f"Offline: {state}"
                            time.sleep(0.5)
                            continue
                        if state == "down":
                            self.note = f"Offline: {iface} is down; run scripts/install_jetson.sh once so adapters come up by themselves"
                            time.sleep(0.5)
                            continue
                        arm.channel = iface
                    if arm.connect():
                        self.note = None
                    else:
                        self.note = arm.note
                time.sleep(0.1)
            elif not arm.torque:
                arm.refresh()
                time.sleep(0.04)
            else:
                time.sleep(0.2)

    def load_at(self, q):
        with self.load_lock:
            predicted = np.asarray(self.load_gravity(np.asarray(q, dtype=float)[:6]), dtype=float)
            if not self.arm.torque:
                return predicted
            residual = self.arm.effort[:6] - np.asarray(self.load_gravity(self.arm.measured[:6]), dtype=float)
        return predicted + np.clip(residual, -C.PEAK_NM["rs-00"], C.PEAK_NM["rs-00"])

    def load_report(self, q):
        load = self.load_at(q)
        arm = self.arm
        rows = []
        for j in range(6):
            hold = float(arm.continuous[j])
            seconds = H.seconds_at(H.TABLES[arm.models[j]]["still"], load[j])
            left = None if seconds == math.inf else max(0.0, (arm.heat.limit - float(arm.heat.used[j])) * seconds)
            rows.append({"joint": j + 1, "nm": round(float(load[j]), 1), "hold_nm": hold, "seconds_before_relax": None if left is None else round(left)})
        worst = max(rows, key=lambda r: abs(r["nm"]) / r["hold_nm"])
        return {"worst": worst, "joints_nm": [r["nm"] for r in rows]}

    @property
    def ready(self):
        return self.arm.torque and not self.arm.fault

    def power_on(self):
        self.arm.enable()

    def power_off(self):
        self.arm.disable()

    def relief_path(self, q_now):
        tip, rotation = self.kin.pose(q_now)
        table = self.robot.table
        height = tip[2] if table.get("front") is None else max(tip[2], float(table["top"]) + C.RELIEF_ABOVE_TABLE)
        target = np.array([min(tip[0], C.RELIEF_X), tip[1], height])
        if np.linalg.norm(target - tip) < 0.01:
            return None
        path = [np.asarray(q_now, dtype=float)]
        seed = path[0]
        for step in np.linspace(0.0, 1.0, 17)[1:]:
            q, err, _ = self.kin.ik(tip + (target - tip) * step, rotation, seed, iterations=30)
            if err > 0.02:
                break
            path.append(q)
            seed = q
        return np.array(path) if len(path) > 1 else None

    def table_hit(self, goal):
        table = self.robot.table
        if table.get("front") is None:
            return None
        start = self.arm.command[:6] if self.arm.torque else self.arm.measured[:6]
        floor = float(table["top"]) + float(table.get("clearance", 0.01))
        already = set()
        for index, (_, q) in enumerate(min_jerk(start, np.clip(goal, self.arm.lower, self.arm.upper), math.radians(90.0), math.radians(360.0), 0.05)):
            links = self.kin.links(q)
            for name, margin in C.TABLE_POINTS:
                if name not in links:
                    continue
                x, z = links[name][0, 3], links[name][2, 3]
                inside = x >= float(table["front"]) and z < floor + margin
                if index == 0 and inside:
                    already.add(name)
                elif inside and name not in already:
                    return f"That move would put the {name.replace('_', ' ')} into the table ({z:.2f} m high, {x:.2f} m out; the table top is {float(table['top']):.2f} m). Go up and over first, then down"
        return None

    def check_table(self, goal):
        problem = self.table_hit(goal)
        if problem:
            raise ValueError(problem)

    def move_tip(self, xyz, rotation=None, speed_deg_s=C.MOVE_DEG_S):
        seed = self.arm.command[:6] if self.arm.torque else self.arm.q
        if rotation is None:
            rotation = self.kin.pose(seed)[1]
        q, err, _ = self.kin.ik(xyz, rotation, seed, iterations=80)
        self.check_table(q)
        duration = self.arm.move(q, speed_deg_s)
        return q, err, duration

    def status(self):
        s = self.arm.status()
        tip, rot = self.kin.pose(self.arm.q)
        s.update({"tip_xyz": [round(float(v), 4) for v in tip], "tip_rotation": np.round(rot, 4).tolist(), "mount": self.mount, "note": s["note"] or self.note, "ready": self.ready, "counterbalance": self.counterbalance})
        s["limits_deg"] = [[round(math.degrees(lo), 1), round(math.degrees(hi), 1)] for lo, hi in zip(self.arm.lower, self.arm.upper)]
        s["seeed_limits_deg"] = [list(p) for p in C.SEEED_LIMITS_DEG]
        return s


class Robot:
    def __init__(self, names=("left", "right"), sim=False):
        self.sim = sim
        self.closing = threading.Event()
        self.ports = C.load("can_ports.json", {})
        factory = None
        if sim:
            from rebot_teleop.simbus import SimFactory
            factory = SimFactory({C.DEFAULT_CAN.get(n, "can0"): C.POSES_DEG["home"][n] + [C.GRIPPER_OPEN_DEG] for n in names})
        self.factory = factory
        self.table = {**C.TABLE_DEFAULT, **C.load("table.json", {})}
        self.units = {name: Unit(name, self, factory) for name in names}
        self.listeners = []
        self.heavy = False
        self.set_heavy(bool(C.load("limits.json", {}).get("heavy", False)), save=False)

    def start(self):
        for unit in self.units.values():
            unit.thread.start()

    def close(self):
        self.closing.set()
        for unit in self.units.values():
            unit.arm.close()

    def unit(self, name):
        if name not in self.units:
            raise ValueError(f"Unknown arm {name}; use one of {', '.join(self.units)}")
        return self.units[name]

    def pick(self, arm=None):
        if arm in (None, "", "both", "all"):
            return list(self.units.values())
        return [self.unit(arm)]

    def power_on(self, arm=None):
        done, notes = [], []
        for unit in self.pick(arm):
            try:
                unit.power_on()
                done.append(unit.name)
            except Exception as error:
                unit.arm._log(f"power on failed: {error}")
                notes.append(f"{unit.name}: {error}")
        if not done and notes:
            raise ValueError("; ".join(notes))
        return {"powered": done, "notes": notes}

    def ensure_powered(self, arm=None):
        powered = []
        for unit in self.pick(arm):
            if not unit.arm.torque and unit.arm.connected:
                try:
                    unit.power_on()
                except Exception:
                    continue
            if unit.ready:
                powered.append(unit.name)
        return powered

    def power_off(self, arm=None):
        for unit in self.pick(arm):
            unit.power_off()
        return {"off": [u.name for u in self.pick(arm)]}

    def stop(self):
        for unit in self.units.values():
            unit.arm.hold()
        for listener in self.listeners:
            listener()

    def clear_faults(self):
        for unit in self.units.values():
            unit.arm.clear_fault()

    def pose_joints(self, pose, name):
        saved = C.load("poses.json", {})
        if pose in saved and name in saved[pose]:
            return np.radians(saved[pose][name])
        if pose in C.POSES_DEG:
            return np.radians(C.POSES_DEG[pose][name])
        raise ValueError(f"Unknown pose {pose}")

    def set_heavy(self, on, save=True):
        self.heavy = bool(on)
        for unit in self.units.values():
            unit.arm.heat.limit = C.HEAT_LOWER_HEAVY if self.heavy else C.HEAT_LOWER
        if save:
            C.save("limits.json", {"heavy": self.heavy})
        return {"heavy": self.heavy, "relax_at_pct": round(C.HEAT_LOWER_HEAVY * 100 if self.heavy else C.HEAT_LOWER * 100)}

    def set_table(self, top=None, front=None):
        if top is not None:
            self.table["top"] = float(top)
        if front is not None:
            self.table["front"] = None if float(front) < 0 else float(front)
        C.save("table.json", self.table)
        return self.table

    def go(self, pose, arm=None, speed_deg_s=C.MOVE_DEG_S):
        longest = 0.0
        for unit in self.pick(arm):
            if unit.ready:
                unit.check_table(self.pose_joints(pose, unit.name))
        for unit in self.pick(arm):
            if unit.ready:
                longest = max(longest, unit.arm.move(self.pose_joints(pose, unit.name), speed_deg_s))
        return longest

    def save_pose(self, pose, arm=None):
        saved = C.load("poses.json", {})
        entry = saved.get(pose, {})
        for unit in self.pick(arm):
            entry[unit.name] = [round(math.degrees(v), 2) for v in unit.arm.q]
        saved[pose] = entry
        C.save("poses.json", saved)
        return entry

    def wait(self, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(u.arm.busy for u in self.units.values()):
            time.sleep(0.01)

    def joints(self):
        return {name: unit.arm.q for name, unit in self.units.items()}

    def snapshot(self):
        out = {}
        for name, unit in self.units.items():
            arm = unit.arm
            out[name] = {"q": arm.q.tolist(), "gripper": float(arm.width), "powered": arm.torque}
        return out

    def twin(self):
        out = {}
        for name, unit in self.units.items():
            q = unit.arm.q if unit.arm.feedback_at else np.radians(C.POSES_DEG["home"][name])
            links = unit.kin.links(q, unit.arm.width if unit.arm.feedback_at else C.GRIPPER_WIDTH_M / 2)
            out[name] = {link: np.round(m[:3, :].reshape(-1), 5).tolist() for link, m in links.items()}
        return out

    def status(self):
        return {"sim": self.sim, "arms": {name: unit.status() for name, unit in self.units.items()}, "can": can_interfaces(), "table": self.table, "heavy": self.heavy}


def clamp(q):
    return np.clip(np.asarray(q, dtype=float), LOWER, UPPER)


__all__ = ["Robot", "top_down", "clamp"]
