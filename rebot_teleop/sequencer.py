import json
import math
import re
import threading
import time
import numpy as np
from rebot_teleop import config as C

FOLDER = C.DATA / "sequences"
SAFE = re.compile(r"[^A-Za-z0-9 _.()-]+")


def clean_name(name):
    name = SAFE.sub("", str(name or "")).strip()[:60]
    if not name:
        raise ValueError("Give the sequence a name")
    return name


def smooth(values, window=5):
    values = np.asarray(values, dtype=float)
    if len(values) < window:
        return values
    kernel = np.ones(window) / window
    padded = np.pad(values, ((window // 2, window // 2), (0, 0)), mode="edge")
    return np.stack([np.convolve(padded[:, i], kernel, mode="valid") for i in range(values.shape[1])], axis=1)


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
        self.stop_flag = threading.Event()
        robot.listeners.append(self.stop_flag.set)

    def list(self):
        out = []
        for path in sorted(FOLDER.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                out.append({"name": data.get("name", path.stem), "steps": len(data.get("steps", [])), "duration_s": round(self.duration(data), 1), "updated": path.stat().st_mtime})
            except (OSError, ValueError):
                continue
        return out

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
                step["grippers"][unit.name] = round(unit.arm.width, 4)
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
        self.recording = {"arms": names, "rate_hz": rate_hz, "t": [], "data": {n: [] for n in names}, "started": time.monotonic()}
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
                rec["data"][name].append([round(math.degrees(v), 3) for v in arm.q] + [round(arm.width, 4)])
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

    def play(self, name=None, sequence=None, loop=None, speed=None, start=0):
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
        self.playing = data.get("name")
        self.player = threading.Thread(target=self._play, args=(data, loop, max(0.05, speed), int(start)), daemon=True)
        self.player.start()
        return {"playing": self.playing, "powered": powered}

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

    def _wait_arms(self, timeout=120):
        end = time.monotonic() + timeout
        while any(u.arm.busy for u in self.robot.units.values()):
            if self.stop_flag.is_set() or time.monotonic() > end:
                return False
            time.sleep(0.01)
        return True

    def _play(self, data, loop, speed, start):
        try:
            while True:
                for index, step in enumerate(data["steps"]):
                    if index < start:
                        continue
                    if self.stop_flag.is_set():
                        return
                    self.progress = {"index": index, "label": step.get("label") or step.get("type"), "of": len(data["steps"])}
                    if not self._run(step, speed):
                        return
                start = 0
                if not loop:
                    return
        except Exception as error:
            self.progress = {"error": str(error)}
            self.robot.stop()
        finally:
            self.playing = None
            if self.progress and "error" not in self.progress:
                self.progress = None

    def _run(self, step, speed):
        kind = step.get("type")
        units = self.robot.units
        if kind == "pose":
            for name, joints in step.get("arms", {}).items():
                unit = units.get(name)
                if unit and unit.ready:
                    unit.arm.move(np.radians(joints), float(step.get("speed_deg_s", C.MOVE_DEG_S)) * speed, C.MOVE_DEG_S2 * speed)
            for name, width in (step.get("grippers") or {}).items():
                unit = units.get(name)
                if unit and unit.ready and width is not None:
                    unit.arm.grip(float(width))
            if not self._wait_arms():
                return False
            return self._sleep(float(step.get("pause_s", 0.0)) / speed)
        if kind == "gripper":
            unit = units.get(step.get("arm"))
            if unit and unit.ready:
                unit.arm.grip(float(step.get("width", 0.0)))
            return self._sleep(0.6 / speed)
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
            return self._track(step, speed)
        return True

    def _track(self, step, speed):
        rate = float(step.get("speed", 1.0)) * speed
        t = (np.asarray(step["t"], dtype=float) - step["t"][0]) / rate
        playing = {}
        for name, rows in step["arms"].items():
            unit = self.robot.units.get(name)
            if not unit or not unit.ready:
                continue
            rows = smooth(np.asarray(rows, dtype=float))
            joints = np.radians(rows[:, :6])
            widths = rows[:, 6]
            unit.arm.move(joints[0], 40.0 * min(speed, 2.0))
            playing[name] = (unit, joints, widths)
        if not playing or not self._wait_arms():
            return bool(not playing)
        for name, (unit, joints, widths) in playing.items():
            unit.arm.play(list(zip(t.tolist(), joints)))
        begin = time.monotonic()
        while time.monotonic() - begin < t[-1]:
            if self.stop_flag.is_set():
                return False
            i = min(int(np.searchsorted(t, time.monotonic() - begin)), len(t) - 1)
            for name, (unit, joints, widths) in playing.items():
                try:
                    unit.arm.grip(float(widths[i]))
                except ValueError:
                    pass
            time.sleep(0.02)
        if not self._wait_arms():
            return False
        return self._sleep(float(step.get("pause_s", 0.0)) / speed)

    def status(self):
        rec = self.recording
        return {
            "recording": None if rec is None else {"arms": rec["arms"], "seconds": round(time.monotonic() - rec["started"], 1), "samples": len(rec["t"])},
            "playing": self.playing, "progress": self.progress,
        }
