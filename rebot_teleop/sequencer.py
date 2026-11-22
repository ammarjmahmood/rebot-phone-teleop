import json
import math
import re
import threading
import time
import numpy as np
from rebot_teleop import config as C
from rebot_teleop.arm import min_jerk

FOLDER = C.DATA / "sequences"
PLAN_DT = 0.1
SAFE = re.compile(r"[^A-Za-z0-9 _.()-]+")


def clean_name(name):
    name = SAFE.sub("", str(name or "")).strip()[:60]
    if not name:
        raise ValueError("Give the sequence a name")
    return name


OVER_DEG = 1.0


def beyond(name, rows, lower, upper):
    rows = np.atleast_2d(np.asarray(rows, dtype=float))[:, :6]
    low, high = np.degrees(lower[:6]), np.degrees(upper[:6])
    out = []
    for joint in range(6):
        least, most = float(rows[:, joint].min()), float(rows[:, joint].max())
        if most > high[joint] + OVER_DEG:
            out.append({"arm": name, "joint": joint + 1, "deg": round(most), "limit": round(float(high[joint]))})
        if least < low[joint] - OVER_DEG:
            out.append({"arm": name, "joint": joint + 1, "deg": round(least), "limit": round(float(low[joint]))})
    return out


def over_text(items):
    return "; ".join(f"{o['arm']} joint {o['joint']} went to {o['deg']} deg but playback stops at {o['limit']}" for o in items)


TRACK_DT = 0.01
TRACK_EASE_S = 0.36
TRACK_SMOOTH_S = 0.2
SMOOTH_RANGE_S = (0.05, 0.5)
TRACK_HELD_S = 0.25
EDITABLE = {"label": None, "speed_deg_s": (5.0, 180.0), "pause_s": (0.0, 120.0), "speed": (0.1, 3.0), "seconds": (0.0, 120.0), "width": (0.0, C.GRIPPER_WIDTH_M), "action": ("up", "down"), "arm": None}


def fresh_samples(t, rows):
    n = len(t)
    if n < 3:
        return t, rows
    same = np.all(rows[1:] == rows[:-1], axis=1)
    keep = np.ones(n, dtype=bool)
    keep[1:] = ~same
    start = 0
    for i in range(1, n + 1):
        if i == n or not same[i - 1]:
            if i - 1 > start and t[i - 1] - t[start] > TRACK_HELD_S:
                keep[i - 1] = True
            start = i
    keep[-1] = True
    return t[keep], rows[keep]


def smoothing(value=None):
    if value is None:
        return TRACK_SMOOTH_S
    return float(min(max(float(value), SMOOTH_RANGE_S[0]), SMOOTH_RANGE_S[1]))


def condition(t, rows, rate=1.0, smooth=None):
    sigma = smoothing(smooth)
    ease = max(TRACK_EASE_S, 3 * sigma)
    t = (np.asarray(t, dtype=float) - t[0]) / max(float(rate), 0.05)
    rows = np.asarray(rows, dtype=float)
    t, rows = fresh_samples(t, rows)
    grid = np.arange(0.0, t[-1] + 2 * ease + TRACK_DT, TRACK_DT)
    shifted = np.clip(grid - ease, 0.0, t[-1])
    values = np.stack([np.interp(shifted, t, rows[:, i]) for i in range(rows.shape[1])], axis=1)
    half = int(round(3 * sigma / TRACK_DT))
    kernel = np.exp(-0.5 * (np.arange(-half, half + 1) * TRACK_DT / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values, ((half, half), (0, 0)), mode="edge")
    return grid, np.stack([np.convolve(padded[:, i], kernel, mode="valid") for i in range(values.shape[1])], axis=1)


class Sequencer:
    def __init__(self, robot, lift=None):
        self.robot = robot
        self.lift = lift
        FOLDER.mkdir(parents=True, exist_ok=True)
        self.recording = None
        self.record_thread = None
        self.player = None
        self.playing = None
        self.progress = None
        self.lanes = {}
        self.gate = None
        self.smooth = None
        self.power_off_after = False
        self.stop_flag = threading.Event()
        self.summaries = {}
        robot.listeners.append(self.stop_flag.set)

    def list(self):
        out, seen = [], {}
        for path in sorted(FOLDER.glob("*.json")):
            try:
                stamp = path.stat().st_mtime_ns
                cached = self.summaries.get(path.name)
                if cached is None or cached[0] != stamp:
                    data = json.loads(path.read_text())
                    cached = (stamp, {"name": data.get("name", path.stem), "steps": len(data.get("steps", [])), "duration_s": round(self.duration(data), 1), "updated": stamp / 1e9, "motions": self._motions(data)})
                seen[path.name] = cached
                out.append(cached[1])
            except (OSError, ValueError):
                continue
        self.summaries = seen
        return out

    def _motions(self, data):
        out = []
        for index, step in enumerate(data.get("steps", [])):
            if step.get("type") != "track":
                continue
            moving = []
            for arm, rows in (step.get("arms") or {}).items():
                rows = np.asarray(rows, dtype=float)
                if len(rows) and float(np.max(np.ptp(rows[:, :6], axis=0))) > 2.0:
                    moving.append(arm)
            out.append({"step": index, "label": step.get("label") or f"Motion {index + 1}", "moves": moving})
        return out

    def light(self, data):
        out = dict(data)
        out["steps"] = []
        for step in data.get("steps", []):
            if step.get("type") == "track":
                step = {**{k: v for k, v in step.items() if k not in ("t", "arms")}, "seconds": round(step["t"][-1] - step["t"][0], 2), "samples": len(step["t"]), "arm_names": list(step["arms"])}
            out["steps"].append(step)
        return out

    def edit(self, name, change):
        data = self.get(name)
        steps = data.setdefault("steps", [])
        op = change.get("op")
        if op == "meta":
            if "loop" in change:
                data["loop"] = bool(change["loop"])
            return self.save(data)
        try:
            index = int(change.get("index"))
        except (TypeError, ValueError):
            raise ValueError("Which step?")
        if not 0 <= index < len(steps):
            raise ValueError("That step is no longer there; the list was refreshed")
        if op == "set":
            for key, value in (change.get("fields") or {}).items():
                if key not in EDITABLE:
                    continue
                allowed = EDITABLE[key]
                if key == "label":
                    value = str(value)[:80]
                elif key == "arm":
                    if value not in self.robot.units and value != "both":
                        raise ValueError("Choose left, right or both")
                elif key == "action":
                    if value not in allowed:
                        raise ValueError("Choose up or down")
                else:
                    value = min(max(float(value), allowed[0]), allowed[1])
                steps[index][key] = value
        elif op == "move":
            to = min(max(int(change.get("to", index)), 0), len(steps) - 1)
            steps.insert(to, steps.pop(index))
        elif op == "delete":
            steps.pop(index)
        elif op == "duplicate":
            steps.insert(index + 1, json.loads(json.dumps(steps[index])))
        else:
            raise ValueError("Unknown change")
        return self.save(data)

    def path(self, name):
        return FOLDER / f"{clean_name(name)}.json"

    def get(self, name):
        path = self.path(name)
        if not path.exists():
            return {"name": clean_name(name), "steps": [], "loop": False, "speed": 1.0}
        return json.loads(path.read_text())

    def save(self, sequence):
        sequence = dict(sequence)
        sequence["name"] = clean_name(sequence.get("name"))
        sequence.setdefault("steps", [])
        sequence.setdefault("loop", False)
        sequence.setdefault("speed", 1.0)
        tmp = self.path(sequence["name"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(sequence))
        tmp.replace(self.path(sequence["name"]))
        return sequence

    def delete(self, name):
        path = self.path(name)
        if path.exists():
            path.unlink()
        return {"deleted": clean_name(name)}

    def rename(self, old, new):
        data = self.get(old)
        data["name"] = clean_name(new)
        self.save(data)
        if clean_name(old) != clean_name(new):
            self.delete(old)
        return data

    def capture(self, arms=None, speed_deg_s=C.MOVE_DEG_S, pause_s=0.0):
        step = {"type": "pose", "arms": {}, "grippers": {}, "speed_deg_s": speed_deg_s, "pause_s": pause_s, "label": time.strftime("Pose %H:%M:%S")}
        for unit in self.robot.pick(arms):
            if unit.arm.connected:
                step["arms"][unit.name] = [round(math.degrees(v), 2) for v in unit.arm.q]
                if unit.arm.has_gripper:
                    step["grippers"][unit.name] = round(unit.arm.width_goal, 4)
        if not step["arms"]:
            raise ValueError("No arm is connected, so there is no pose to save")
        return step

    def add(self, name, step, index=None):
        data = self.get(name)
        if index is None or index >= len(data["steps"]):
            data["steps"].append(step)
        else:
            data["steps"].insert(max(0, int(index)), step)
        return self.save(data)

    def start_recording(self, arms=None, rate_hz=50.0):
        if self.recording is not None:
            raise ValueError("Already recording")
        names = [u.name for u in self.robot.pick(arms) if u.arm.connected]
        if not names:
            raise ValueError("No arm is connected to record")
        self.recording = {"arms": names, "rate_hz": rate_hz, "t": [], "data": {n: [] for n in names}, "over": {}, "no_gripper": set(), "started": time.monotonic()}
        self.record_thread = threading.Thread(target=self._record, daemon=True)
        self.record_thread.start()
        return {"recording": names}

    def _record(self):
        rec = self.recording
        period = 1.0 / rec["rate_hz"]
        while self.recording is rec:
            t = time.monotonic() - rec["started"]
            rec["t"].append(round(t, 4))
            for name in rec["arms"]:
                arm = self.robot.units[name].arm
                if not arm.has_gripper:
                    rec["no_gripper"].add(name)
                row = [round(math.degrees(v), 3) for v in arm.q] + [round(arm.width_goal, 4)]
                rec["data"][name].append(row)
                for item in beyond(name, [row], arm.lower, arm.upper):
                    key = (name, item["joint"], item["limit"])
                    old = rec["over"].get(key)
                    if old is None or abs(item["deg"] - item["limit"]) > abs(old["deg"] - old["limit"]):
                        rec["over"][key] = item
            time.sleep(period)

    def stop_recording(self, name=None, label=None):
        rec = self.recording
        if rec is None:
            raise ValueError("Not recording")
        self.recording = None
        if self.record_thread:
            self.record_thread.join(timeout=1)
        if len(rec["t"]) < 3:
            raise ValueError("The recording is too short")
        step = {"type": "track", "label": label or time.strftime("Motion %H:%M:%S"), "rate_hz": rec["rate_hz"], "t": rec["t"], "arms": rec["data"], "speed": 1.0, "pause_s": 0.0}
        if rec["over"]:
            step["over"] = list(rec["over"].values())
        if rec["no_gripper"]:
            step["no_gripper"] = sorted(rec["no_gripper"])
        if name:
            self.add(name, step)
        return step

    def duration(self, data):
        total = 0.0
        for step in data.get("steps", []):
            kind = step.get("type")
            if kind == "track":
                total += (step["t"][-1] - step["t"][0]) / max(float(step.get("speed", 1.0)), 0.05) + 1.0
            elif kind == "pose":
                total += 1.5 + float(step.get("pause_s", 0))
            elif kind in ("wait", "lift"):
                total += float(step.get("seconds", 0))
            else:
                total += 0.5
        return total / max(float(data.get("speed", 1.0)), 0.05)

    def play(self, name=None, sequence=None, loop=None, speed=None, start=0, smooth=None, power_off=False):
        if self.player and self.player.is_alive():
            raise ValueError("A sequence is already playing; stop it first")
        data = sequence or self.get(name)
        if not data.get("steps"):
            raise ValueError("That sequence has no steps")
        loop = data.get("loop", False) if loop is None else bool(loop)
        speed = float(data.get("speed", 1.0) if speed is None else speed)
        powered = self.robot.ensure_powered()
        if not powered:
            raise ValueError("No arm could be powered; check the arm power and CAN cables")
        self.stop_flag.clear()
        self.lanes = {}
        self.smooth = smoothing(data.get("smooth") if smooth is None else smooth)
        self.power_off_after = bool(power_off)
        self.playing = data.get("name")
        self.player = threading.Thread(target=self._play, args=(data, loop, max(0.05, speed), int(start)), daemon=True)
        self.player.start()
        return {"playing": self.playing, "powered": powered}

    def play_together(self, parts, loop=False, speed=1.0, smooth=None, power_off=False):
        if self.player and self.player.is_alive():
            raise ValueError("A sequence is already playing; stop it first")
        jobs = []
        for arm, part in parts.items():
            if not part:
                continue
            if arm not in self.robot.units:
                raise ValueError(f"Unknown arm {arm}")
            name, step = (part.get("name"), part.get("step")) if isinstance(part, dict) else (part, None)
            data = self.get(name)
            if step is not None:
                step = int(step)
                if not 0 <= step < len(data.get("steps", [])):
                    raise ValueError(f"{name} has no step {step + 1}")
                chosen = data["steps"][step]
                data = dict(data, name=f"{name} step {step + 1} ({chosen.get('label') or chosen.get('type')})", steps=[chosen])
            if not any(self._touches(step, arm) for step in data.get("steps", [])):
                raise ValueError(f"{name} has no movement for the {arm} arm; record it with Arms set to {arm.capitalize()} or Both")
            jobs.append((arm, data))
        if len(jobs) < 2:
            raise ValueError("Pick a recording for each arm")
        powered = self.robot.ensure_powered()
        missing = [arm for arm, _ in jobs if arm not in powered]
        if missing:
            raise ValueError(f"The {' and '.join(missing)} {'arms are' if len(missing) > 1 else 'arm is'} not powered; check the arm power and CAN cables")
        self.stop_flag.clear()
        self.lanes = {}
        self.progress = None
        self.smooth = smoothing(smooth)
        self.power_off_after = bool(power_off)
        self.playing = " + ".join(f"{data.get('name')} ({arm})" for arm, data in jobs)
        speed = max(0.05, float(speed))
        self.player = threading.Thread(target=self._together, args=(jobs, bool(loop), speed), daemon=True)
        self.player.start()
        return {"playing": self.playing, "powered": powered}

    def _touches(self, step, arm):
        kind = step.get("type")
        if kind in ("pose", "track"):
            return arm in (step.get("arms") or {}) or arm in (step.get("grippers") or {})
        if kind == "gripper":
            return step.get("arm") in (None, "", "both", arm)
        return False

    def _together(self, jobs, loop, speed):
        errors = []
        start = threading.Barrier(len(jobs))
        self.gate = {"need": len(jobs), "arrived": set(), "cv": threading.Condition()}

        def lane(arm, data):
            try:
                start.wait(timeout=5)
                self._steps(data, loop, speed, 0, only={arm}, lane=arm)
            except Exception as error:
                errors.append(f"{arm} arm: {error}")
                self.stop_flag.set()
            finally:
                self._pass_gate(arm, wait=False)
        threads = [threading.Thread(target=lane, args=job, daemon=True) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            self.progress = {"error": "; ".join(errors)}
            self.robot.stop()
        else:
            self._power_off_when_done([arm for arm, _ in jobs])
            self.progress = None
        self.lanes = {}
        self.gate = None
        self.playing = None

    def _pass_gate(self, lane, wait=True):
        gate = self.gate
        if gate is None or lane is None:
            return True
        with gate["cv"]:
            if lane in gate["arrived"]:
                return True
            gate["arrived"].add(lane)
            gate["cv"].notify_all()
            if not wait:
                return True
            end = time.monotonic() + C.REST_TIMEOUT_S
            while len(gate["arrived"]) < gate["need"] and not self.stop_flag.is_set() and time.monotonic() < end:
                gate["cv"].wait(0.05)
        return not self.stop_flag.is_set()

    def _set_progress(self, lane, progress):
        if lane is None:
            self.progress = progress
            return
        self.lanes[lane] = progress
        lanes = list(self.lanes.items())
        label = " | ".join(f"{arm}: step {p['index'] + 1} of {p['of']} ({p['label']})" for arm, p in lanes)
        self.progress = {"index": min(p["index"] for _, p in lanes), "of": max(p["of"] for _, p in lanes), "label": label, "lanes": dict(lanes)}

    def _units(self, only):
        return {n: u for n, u in self.robot.units.items() if only is None or n in only}

    def stop(self):
        self.stop_flag.set()
        self.robot.stop()
        return {"stopped": True}

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_flag.is_set():
                return False
            time.sleep(0.01)
        return True

    def _wait_arms(self, timeout=120, only=None):
        end = time.monotonic() + timeout
        rest_end = time.monotonic() + C.REST_TIMEOUT_S
        while True:
            arms = [u.arm for u in self._units(only).values()]
            resting = any(a.resting for a in arms)
            if not resting and not any(a.busy for a in arms):
                return True
            if self.stop_flag.is_set() or time.monotonic() > (rest_end if resting else end):
                return False
            time.sleep(0.01)

    def _wait_grippers(self, names, timeout=4.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.stop_flag.is_set():
                return False
            arms = [self.robot.units[n].arm for n in names if n in self.robot.units and self.robot.units[n].ready]
            if all(a.gripper_stalled or abs(a.target[6] - a.command[6]) < 1e-3 for a in arms if a.has_gripper):
                return self._sleep(0.15)
            time.sleep(0.01)
        return True

    def _gravity(self, name):
        if not hasattr(self, "gravities"):
            self.gravities = {}
        if name not in self.gravities:
            self.gravities[name] = self.robot.units[name].kin.gravity_function()
        return self.gravities[name]

    def _paths(self, step, speed, current):
        kind = step.get("type")
        paths, hold = {}, 0.0
        if kind == "pose":
            for name, joints in step.get("arms", {}).items():
                if name in current:
                    arm = self.robot.units[name].arm
                    goal = np.clip(np.radians(joints), arm.lower, arm.upper)
                    paths[name] = [q for _, q in min_jerk(current[name], goal, math.radians(float(step.get("speed_deg_s", C.MOVE_DEG_S)) * speed), math.radians(C.MOVE_DEG_S2 * speed), PLAN_DT)]
            hold = float(step.get("pause_s", 0.0)) / speed
        elif kind == "gripper":
            hold = 0.6 / speed
        elif kind == "wait":
            hold = float(step.get("seconds", 1.0)) / speed
        elif kind == "lift":
            hold = float(step.get("seconds", 1.0))
        elif kind == "track":
            rate = float(step.get("speed", 1.0)) * speed
            every = int(round(PLAN_DT / TRACK_DT))
            for name, rows in step["arms"].items():
                if name not in current:
                    continue
                arm = self.robot.units[name].arm
                joints = np.clip(np.radians(condition(step["t"], rows, rate, self.smooth)[1][:, :6]), arm.lower, arm.upper)
                approach = [q for _, q in min_jerk(current[name], joints[0], math.radians(40.0 * min(speed, 2.0)), math.radians(C.MOVE_DEG_S2), PLAN_DT)]
                paths[name] = approach + list(joints[::every])
            hold = float(step.get("pause_s", 0.0)) / speed
        count = max([len(p) for p in paths.values()] + [1]) + int(math.ceil(hold / PLAN_DT))
        out = {}
        for name, q in current.items():
            path = paths.get(name) or [q]
            out[name] = [np.asarray(path[min(i, len(path) - 1)], dtype=float) for i in range(count)]
        return out, count * PLAN_DT

    def _use(self, name, budget, path):
        gravity = self._gravity(name)
        used = budget.used.copy()
        peak = used.copy()
        torque_peak = np.zeros(6)
        for i, q in enumerate(path):
            torque = np.abs(np.asarray(gravity(q), dtype=float)) * C.HEAT_MODEL_MARGIN
            torque_peak = np.maximum(torque_peak, torque)
            moved = np.degrees(np.abs(q - path[i - 1])) / PLAN_DT if i else np.zeros(6)
            budget.step(torque, moved, PLAN_DT)
            peak = np.maximum(peak, budget.used)
        rise = peak - used
        return rise, torque_peak

    def _rest_target(self, rise):
        return float(np.min(C.HEAT_PLAN_LIMIT - rise))

    def _too_much(self, rise, torque, name, index, step):
        joint = int(np.argmax(rise))
        label = step.get("label") or step.get("type")
        return f"Step {index + 1} ({label}) asks joint {joint + 1} of the {name} arm to hold about {torque[joint]:.0f} Nm for longer than it can, even fully rested. Shorten its pause, speed it up, or bring that pose closer in or lower"

    def plan(self, data, speed=None):
        speed = max(0.05, float(data.get("speed", 1.0) if speed is None else speed))
        current, budgets = {}, {}
        for name, unit in self.robot.units.items():
            arm = unit.arm
            if not arm.connected:
                continue
            current[name] = (arm.command[:6] if arm.torque else arm.measured[:6]).copy()
            budgets[name] = arm.heat.copy()
            if not arm.torque:
                budgets[name].used[:] = 0.0
        steps, total, resting, problems, warnings = [], 0.0, 0.0, [], []
        for index, step in enumerate(data.get("steps", [])):
            paths, seconds = self._paths(step, speed, current)
            entry = {"index": index, "label": step.get("label") or step.get("type"), "seconds": round(seconds, 1), "rest_s": 0, "peak": {}}
            over = self._over(step)
            if over:
                entry["over"] = over
                warnings.append(f"Step {index + 1} ({entry['label']}) will be cut short: {over_text(over)}")
            rest = 0.0
            for name, path in paths.items():
                rise, torque = self._use(name, budgets[name].copy(), path)
                target = self._target_or_none(rise)
                if target is None:
                    entry["problem"] = self._too_much(rise, torque, name, index, step)
                    problems.append(entry["problem"])
                    continue
                over = float(np.max(budgets[name].used)) - target
                if over > 0:
                    rest = max(rest, over * C.HEAT_COOL_S)
            if rest:
                entry["rest_s"] = round(rest)
                for budget in budgets.values():
                    budget.cool(rest)
            for name, path in paths.items():
                self._use(name, budgets[name], path)
                worst = int(np.argmax(budgets[name].used))
                entry["peak"][name] = {"joint": worst + 1, "pct": round(float(budgets[name].used[worst]) * 100)}
                current[name] = path[-1]
            total += seconds + rest
            resting += rest
            steps.append(entry)
        return {"ok": not problems or self.robot.heavy, "heavy": self.robot.heavy, "seconds": round(total, 1), "rest_s": 0 if self.robot.heavy else round(resting), "steps": steps, "problems": problems, "warnings": warnings, "speed": speed}

    def _over(self, step):
        if step.get("type") not in ("pose", "track"):
            return []
        out = []
        for name, rows in (step.get("arms") or {}).items():
            unit = self.robot.units.get(name)
            if unit is None or not rows:
                continue
            out += beyond(name, rows, unit.arm.lower, unit.arm.upper)
        return out

    def _target_or_none(self, rise):
        target = self._rest_target(rise)
        return None if target < 0 else target

    def _prepare(self, index, step, speed, only=None, lane=None):
        if self.robot.heavy:
            return True
        current = {n: u.arm.command[:6].copy() for n, u in self._units(only).items() if u.arm.torque}
        if not current:
            return True
        paths, _ = self._paths(step, speed, current)
        rested = False
        for name, path in paths.items():
            arm = self.robot.units[name].arm
            rise, torque = self._use(name, arm.heat.copy(), path)
            target = self._target_or_none(rise)
            if target is None:
                raise ValueError(self._too_much(rise, torque, name, index, step))
            if float(np.max(arm.heat.used)) > target:
                joint = int(np.argmax(arm.heat.used)) + 1
                label = step.get("label") or step.get("type")
                previous = self.lanes.get(lane) if lane else self.progress
                self._set_progress(lane, {"index": index, "label": f"resting joint {joint} of the {name} arm before {label}", "of": previous["of"] if previous and "of" in previous else index + 1})
                rested = arm.relax(f"Resting before step {index + 1} so joint {joint} has enough left for it", resume=target) or rested
        if rested:
            return self._wait_arms(only=only)
        return True

    def _power_off_when_done(self, names):
        if not self.power_off_after or self.stop_flag.is_set() or not names:
            return
        if not self._wait_arms(timeout=10, only=set(names)) or self.stop_flag.is_set():
            return
        for name in names:
            print(f"{time.strftime('%H:%M:%S')} {name} arm: playback finished; powering off as asked", flush=True)
            self.robot.power_off(name)

    def _play(self, data, loop, speed, start):
        try:
            used = [n for n, u in self.robot.units.items() if u.arm.torque]
            self._steps(data, loop, speed, start)
            self._power_off_when_done(used)
        except Exception as error:
            self.progress = {"error": str(error)}
            self.robot.stop()
        finally:
            self.playing = None
            if self.progress and "error" not in self.progress:
                self.progress = None

    def _steps(self, data, loop, speed, start, only=None, lane=None):
        units = self._units(only)
        while True:
            for index, step in enumerate(data["steps"]):
                if index < start:
                    continue
                if self.stop_flag.is_set():
                    return
                if only is not None and step.get("type") not in ("wait", "lift") and not any(self._touches(step, n) for n in only):
                    continue
                self._set_progress(lane, {"index": index, "label": step.get("label") or step.get("type"), "of": len(data["steps"])})
                for attempt in range(3):
                    if not self._prepare(index, step, speed, only, lane):
                        return
                    self._set_progress(lane, {"index": index, "label": step.get("label") or step.get("type"), "of": len(data["steps"])})
                    before = {n: u.arm.rests for n, u in units.items()}
                    if not self._run(step, speed, only, lane):
                        return
                    if all(u.arm.rests == before[n] for n, u in units.items()):
                        break
                    if not self._wait_arms(only=only):
                        return
                else:
                    raise ValueError(f"Step {index + 1} kept running out of motor budget; it needs a lighter pose or a shorter pause")
            start = 0
            if not loop:
                return

    def _run(self, step, speed, only=None, lane=None):
        kind = step.get("type")
        units = self._units(only)
        if kind == "pose":
            for name, joints in step.get("arms", {}).items():
                unit = units.get(name)
                if unit and unit.ready:
                    unit.arm.move(np.radians(joints), float(step.get("speed_deg_s", C.MOVE_DEG_S)) * speed, C.MOVE_DEG_S2 * speed)
            for name, width in (step.get("grippers") or {}).items():
                unit = units.get(name)
                if unit and unit.ready and width is not None:
                    unit.arm.grip(float(width))
            if not self._wait_arms(only=only) or not self._wait_grippers([n for n in (step.get("grippers") or {}) if n in units]):
                return False
            return self._sleep(float(step.get("pause_s", 0.0)) / speed)
        if kind == "gripper":
            for name in (units if step.get("arm") in (None, "", "both") else [step.get("arm")]):
                unit = units.get(name)
                if unit and unit.ready:
                    unit.arm.grip(float(step.get("width", 0.0)))
            return self._wait_grippers(list(units))
        if kind == "wait":
            return self._sleep(float(step.get("seconds", 1.0)) / speed)
        if kind == "lift":
            if self.lift is None:
                return True
            self.lift.command(step.get("action", "stop"))
            ok = self._sleep(float(step.get("seconds", 1.0)))
            self.lift.command("stop")
            return ok
        if kind == "track":
            return self._track(step, speed, only, lane)
        return True

    def _track(self, step, speed, only=None, lane=None):
        rate = float(step.get("speed", 1.0)) * speed
        t = None
        playing = {}
        for name, rows in step["arms"].items():
            unit = self._units(only).get(name)
            if not unit or not unit.ready:
                continue
            t, rows = condition(step["t"], rows, rate, self.smooth)
            joints = np.radians(rows[:, :6])
            widths = None if name in (step.get("no_gripper") or []) else rows[:, 6]
            unit.arm.move(joints[0], 40.0 * min(speed, 2.0))
            if widths is not None:
                try:
                    unit.arm.grip(float(widths[0]))
                except ValueError:
                    pass
            playing[name] = (unit, joints, widths)
        if not playing or not self._wait_arms(only=only):
            return bool(not playing)
        if not self._pass_gate(lane):
            return False
        for name, (unit, joints, widths) in playing.items():
            unit.arm.play(list(zip(t.tolist(), joints)))
        begin = time.monotonic()
        while time.monotonic() - begin < t[-1]:
            if self.stop_flag.is_set():
                return False
            i = min(int(np.searchsorted(t, time.monotonic() - begin)), len(t) - 1)
            for name, (unit, joints, widths) in playing.items():
                if widths is None:
                    continue
                try:
                    unit.arm.grip(float(widths[i]))
                except ValueError:
                    pass
            time.sleep(0.02)
        if not self._wait_arms(only=only):
            return False
        return self._sleep(float(step.get("pause_s", 0.0)) / speed)

    def status(self):
        rec = self.recording
        return {
            "recording": None if rec is None else {"arms": rec["arms"], "seconds": round(time.monotonic() - rec["started"], 1), "samples": len(rec["t"]), "over": list(rec["over"].values())},
            "playing": self.playing, "progress": self.progress,
        }
