import collections
import json
import math
import threading
import time
from rebot_teleop import config as C
from rebot_teleop import heat as H

BIN_DEG = 15.0


class MotorLog:
    def __init__(self, robot, root=None, hz=None, keep_days=None, mode=None):
        self.robot = robot
        self.root = root or C.DATA / "motorlog"
        self.hz = hz or C.MOTORLOG_HZ
        self.keep_days = keep_days or C.MOTORLOG_KEEP_DAYS
        self.mode = mode or (lambda: "idle")
        self.lock = threading.Lock()
        self.running = threading.Event()
        self.thread = None
        self.day = None
        self.handle = None
        self.rows = 0
        for unit in robot.units.values():
            unit.arm.on_event = self.event

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.prune()
        self.running.set()
        self.thread = threading.Thread(target=self._loop, name="motorlog", daemon=True)
        self.thread.start()

    def stop(self):
        self.running.clear()
        if self.thread:
            self.thread.join(timeout=2)
        with self.lock:
            if self.handle:
                self.handle.close()
                self.handle = None

    def _file(self):
        day = time.strftime("%Y-%m-%d")
        if day != self.day or self.handle is None:
            if self.handle:
                self.handle.close()
            self.root.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.root / f"{day}.jsonl", "a", buffering=1)
            if self.day is not None:
                self.prune()
            self.day = day
        return self.handle

    def _write(self, row):
        with self.lock:
            self._file().write(json.dumps(row, separators=(",", ":")) + "\n")
            self.rows += 1

    def _snapshot(self, name, arm):
        return {
            "t": round(time.time(), 2), "arm": name,
            "q": [round(math.degrees(v), 1) for v in arm.measured[:6]],
            "nm": [round(float(v), 2) for v in arm.effort[:6]],
            "vel": [round(math.degrees(v), 1) for v in arm.velocity[:6]],
            "heat": [round(float(v) * 100, 1) for v in arm.heat.used],
        }

    def _loop(self):
        period = 1.0 / self.hz
        while self.running.is_set():
            started = time.monotonic()
            try:
                mode = self.mode()
                for name, unit in self.robot.units.items():
                    arm = unit.arm
                    if not arm.torque:
                        continue
                    row = self._snapshot(name, arm)
                    row["mode"] = mode
                    if arm.resting:
                        row["rest"] = arm.rest_phase
                    if arm.fault and not arm.resting:
                        row["fault"] = arm.fault[:80]
                    self._write(row)
            except Exception as error:
                print("motor log failed:", error, flush=True)
            time.sleep(max(0.0, period - (time.monotonic() - started)))

    def event(self, name, text):
        unit = self.robot.units.get(name)
        if unit is None or text.endswith("status code 0"):
            return
        row = self._snapshot(name, unit.arm)
        row["event"] = text.split("; joints ")[0]
        try:
            self._write(row)
        except Exception as error:
            print("motor log event failed:", error, flush=True)

    def prune(self):
        cutoff = time.time() - self.keep_days * 86400
        for path in self.root.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def rows_since(self, seconds, arm=None):
        start = time.time() - seconds
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                if path.stat().st_mtime < start:
                    continue
                with open(path) as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if row.get("t", 0) >= start and (arm is None or row.get("arm") == arm):
                            yield row
            except OSError:
                continue

    def report(self, hours=24.0, arm=None, top=8):
        models = {name: unit.arm.models for name, unit in self.robot.units.items()}
        hold = {name: unit.arm.continuous.tolist() for name, unit in self.robot.units.items()}
        dt = 1.0 / self.hz
        arms = {}
        events = []
        for row in self.rows_since(hours * 3600.0, arm):
            name = row["arm"]
            if "event" in row:
                events.append({k: row[k] for k in ("t", "arm", "event", "q", "nm", "heat")})
                continue
            if name not in models:
                continue
            a = arms.setdefault(name, {"seconds": 0.0, "over": [0.0] * 6, "peak": [None] * 6, "bins": collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]), "modes": collections.Counter(), "rest_s": 0.0})
            a["seconds"] += dt
            a["modes"][row.get("mode", "idle")] += dt
            if row.get("rest"):
                a["rest_s"] += dt
            nm, vel, q = row["nm"], row.get("vel", [0.0] * 6), row["q"]
            for j in range(6):
                if abs(nm[j]) > hold[name][j]:
                    a["over"][j] += dt
                if a["peak"][j] is None or abs(nm[j]) > abs(a["peak"][j]["nm"]):
                    a["peak"][j] = {"nm": nm[j], "q": q, "t": row["t"]}
            key = tuple(int(round(v / BIN_DEG) * BIN_DEG) for v in q[:3])
            rate = max(0.0, H.heat_rate(models[name][0], nm[0], vel[0]))
            b = a["bins"][key]
            b[0] += dt
            b[1] += abs(nm[0]) * dt
            b[2] += rate * dt
            b[3] += dt if abs(nm[0]) > hold[name][0] else 0.0
        out = {"hours": hours, "hz": self.hz, "arms": {}, "events": events[-50:]}
        for name, a in arms.items():
            bins = []
            for key, (seconds, nm_s, used, over_s) in a["bins"].items():
                if seconds < 2.0:
                    continue
                bins.append({"j1_j2_j3_deg": list(key), "seconds": round(seconds, 1), "j1_mean_nm": round(nm_s / seconds, 2), "j1_budget_pct_per_min": round(used / seconds * 6000, 1), "j1_over_hold_s": round(over_s, 1)})
            costly = sorted([b for b in bins if b["j1_budget_pct_per_min"] > 0.5 or b["j1_over_hold_s"] > 0], key=lambda b: -b["j1_budget_pct_per_min"] * b["seconds"])[:top]
            cheap = sorted([b for b in bins if b["seconds"] >= 5.0], key=lambda b: b["j1_mean_nm"])[:top]
            out["arms"][name] = {
                "powered_min": round(a["seconds"] / 60, 1), "relaxing_min": round(a["rest_s"] / 60, 1),
                "minutes_by_mode": {k: round(v / 60, 1) for k, v in a["modes"].items()},
                "minutes_over_hold": [round(v / 60, 2) for v in a["over"]], "hold_nm": hold[name],
                "peak": [None if p is None else {"nm": p["nm"], "joints_deg": p["q"], "at": time.strftime("%H:%M:%S", time.localtime(p["t"]))} for p in a["peak"]],
                "costly_poses": costly, "cheap_poses": cheap,
            }
        return out


def report_text(report):
    lines = [f"Motor report, last {report['hours']:g} h"]
    for name, a in report["arms"].items():
        lines.append("")
        lines.append(f"{name} arm: powered {a['powered_min']} min, relaxing {a['relaxing_min']} min, " + ", ".join(f"{k} {v} min" for k, v in a["minutes_by_mode"].items()))
        lines.append("  minutes above what each joint holds while still: " + "  ".join(f"J{j + 1} {m}" for j, m in enumerate(a["minutes_over_hold"])))
        for j, p in enumerate(a["peak"][:3]):
            if p:
                lines.append(f"  J{j + 1} peak {p['nm']:+.1f} Nm at {p['at']}, joints {p['joints_deg']}")
        lines.append("  poses that use joint 1's budget most (J1, J2, J3 in 15 deg steps):")
        for b in a["costly_poses"]:
            lines.append(f"    {b['j1_j2_j3_deg']}: {b['seconds']} s there, J1 {b['j1_mean_nm']} Nm average, {b['j1_budget_pct_per_min']}% of the budget per minute")
        lines.append("  poses that cost joint 1 least:")
        for b in a["cheap_poses"]:
            lines.append(f"    {b['j1_j2_j3_deg']}: {b['seconds']} s there, J1 {b['j1_mean_nm']} Nm average")
    if report["events"]:
        lines.append("")
        lines.append("Events:")
        for e in report["events"]:
            lines.append(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(e['t']))} {e['arm']}: {e['event']}; joints {e['q']} deg, torque {e['nm']} Nm, budget {e['heat']}%")
    return "\n".join(lines)
