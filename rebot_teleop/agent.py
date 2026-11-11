import math
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import numpy as np
import httpx

DEFAULT_URL = os.environ.get("REBOT_URL", "http://127.0.0.1:8081")

API = """robot = Robot()  (already made for you in run_policy)

State
  robot.state()                      dict per arm: joints_deg, tip_xyz, gripper_m, powered, note, budget
  robot.budget(arm)                  motor budget: worst joint, pct used, seconds left before the arm relaxes itself
  robot.wait_ready(arm=None)         waits while an arm relaxes to recover, then returns

Motor budget: the shoulder (joint 1) can hold about 8 Nm while still. Reaching far out uses it up in minutes,
then the arm relaxes at home, recovers and goes back up by itself. Work fast, keep poses close in or low,
and do not park with the arm stretched out. The hanging home pose costs almost nothing.
  robot.tip(arm) -> np.array xyz     robot.joints(arm) -> degrees

Power (holds still where the arm is; nothing moves)
  robot.power_on(arm=None)           robot.power_off(arm=None)

Move (smooth, waits until done unless wait=False; speed in deg/s, default 90)
  robot.move_joints(arm, [6 deg], speed=90)
  robot.move_tip(arm, xyz, rotation=None)      rotation: None keeps the hand angle, "down", a yaw in degrees for top down, or a 3x3 matrix
  robot.move_tip_by(arm, [dx, dy, dz])
  robot.go("home" | "ready" | saved pose name, arm=None)
  robot.can_reach(arm, xyz, rotation=None) -> dict
  robot.grip(arm, width_m)   robot.open(arm)   robot.close(arm)      width 0 closed to 0.05 open
  robot.pick(arm, xyz, yaw_deg=0, approach=0.08, lift=0.10)
  robot.place(arm, xyz, yaw_deg=0, approach=0.08)
  robot.stop()                       holds every arm where it is

Sequences and the table
  robot.play(name, wait=True)        plays a sequence saved in the Sequencer tab
  robot.lift(action, seconds)        action up or down, then stops

Cameras: "left", "right" (wrists), "head" (fixed HP60C with depth)
  robot.capture(camera) -> BGR numpy image
  robot.locate(camera, [[u, v], ...], plane_z=None) -> list of world xyz (head uses depth)
  robot.find_color(camera, "red" | "green" | "blue" | "yellow" | "orange") -> list of {pixel, xyz, area}

World frame: metres, origin at the foot of the post, x forward, y to the robot's left, z up.
The left arm works at y > 0 and the right arm at y < 0. Out of reach targets go as close as possible and report the error."""

COLORS = {"red": [((0, 120, 70), (10, 255, 255)), ((170, 120, 70), (180, 255, 255))], "orange": [((10, 120, 80), (22, 255, 255))], "yellow": [((22, 100, 100), (35, 255, 255))], "green": [((36, 80, 50), (85, 255, 255))], "blue": [((90, 100, 50), (130, 255, 255))]}


class RobotError(RuntimeError):
    pass


class Robot:
    def __init__(self, url=None, timeout=120.0):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.client = httpx.Client(base_url=self.url, timeout=timeout)

    def _call(self, method, path, body=None):
        for attempt in range(3):
            try:
                response = self.client.request(method, path, json=body)
                break
            except httpx.TransportError as error:
                if attempt == 2:
                    raise RobotError(f"Cannot reach the robot server at {self.url}: {error}")
                time.sleep(1.0)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text
            raise RobotError(str(detail))
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    def state(self):
        arms = self._call("GET", "/api/status")["arms"]
        return {name: {"joints_deg": a["joints_deg"], "tip_xyz": a["tip_xyz"], "gripper_m": a["gripper_m"], "powered": a["powered"], "connected": a["connected"], "note": a["note"] or a["fault"], "budget": a.get("heat")} for name, a in arms.items()}

    def budget(self, arm):
        return self._call("GET", "/api/status")["arms"][arm].get("heat")

    def wait_ready(self, arm=None, timeout_s=600.0):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            arms = self._call("GET", "/api/status")["arms"]
            if not any(a.get("rest") for name, a in arms.items() if arm in (None, name)):
                return True
            time.sleep(1.0)
        return False

    def tip(self, arm):
        return np.asarray(self.state()[arm]["tip_xyz"])

    def joints(self, arm):
        return np.asarray(self.state()[arm]["joints_deg"])

    def power_on(self, arm=None):
        return self._call("POST", "/api/power", {"arm": arm, "on": True})

    def power_off(self, arm=None):
        return self._call("POST", "/api/power", {"arm": arm, "on": False})

    def _rot(self, rotation):
        if rotation is None or isinstance(rotation, (str, int, float)):
            return rotation
        return np.asarray(rotation, dtype=float).tolist()

    def move_joints(self, arm, joints_deg, speed=90.0, wait=True):
        return self._call("POST", "/api/move", {"arm": arm, "joints_deg": list(map(float, joints_deg)), "speed_deg_s": speed, "wait": wait})

    def move_tip(self, arm, xyz, rotation=None, speed=90.0, wait=True):
        return self._call("POST", "/api/move", {"arm": arm, "tip_xyz": list(map(float, xyz)), "rotation": self._rot(rotation), "speed_deg_s": speed, "wait": wait})

    def move_tip_by(self, arm, delta, rotation=None, speed=90.0, wait=True):
        return self._call("POST", "/api/move", {"arm": arm, "tip_delta": list(map(float, delta)), "rotation": self._rot(rotation), "speed_deg_s": speed, "wait": wait})

    def go(self, pose, arm=None, speed=90.0, wait=True):
        return self._call("POST", "/api/move", {"pose": pose, "arm": arm, "speed_deg_s": speed, "wait": wait})

    def home(self):
        return self.go("home")

    def ready(self):
        return self.go("ready")

    def can_reach(self, arm, xyz, rotation=None):
        return self._call("POST", "/api/reach", {"arm": arm, "tip_xyz": list(map(float, xyz)), "rotation": self._rot(rotation)})

    def grip(self, arm, width_m):
        self._call("POST", "/api/gripper", {"arm": arm, "width_m": float(width_m)})
        time.sleep(0.6)

    def open(self, arm):
        self.grip(arm, 0.05)

    def close(self, arm):
        self.grip(arm, 0.0)

    def stop(self):
        return self._call("POST", "/api/stop")

    def top_down(self, yaw_deg=0.0):
        from rebot_teleop.kin import top_down
        return top_down(yaw_deg)

    def pick(self, arm, xyz, yaw_deg=0.0, approach=0.08, lift=0.10):
        xyz = np.asarray(xyz, dtype=float)
        self.open(arm)
        self.move_tip(arm, xyz + [0, 0, approach], yaw_deg)
        self.move_tip(arm, xyz, yaw_deg, speed=25)
        self.close(arm)
        return self.move_tip(arm, xyz + [0, 0, lift], yaw_deg, speed=30)

    def place(self, arm, xyz, yaw_deg=0.0, approach=0.08):
        xyz = np.asarray(xyz, dtype=float)
        self.move_tip(arm, xyz + [0, 0, approach], yaw_deg)
        self.move_tip(arm, xyz, yaw_deg, speed=25)
        self.open(arm)
        return self.move_tip(arm, xyz + [0, 0, approach], yaw_deg, speed=30)

    def play(self, name, wait=True):
        result = self._call("POST", f"/api/seq/{name}/play", {})
        while wait:
            time.sleep(0.2)
            if not self._call("GET", "/api/seq")["playing"]:
                break
        return result

    def lift(self, action, seconds=1.0):
        self._call("POST", "/api/lift", {"action": action})
        time.sleep(float(seconds))
        return self._call("POST", "/api/lift", {"action": "stop"})

    def capture(self, camera):
        import cv2
        data = self._call("GET", f"/api/camera/{camera}.jpg")
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    def jpeg(self, camera):
        return self._call("GET", f"/api/camera/{camera}.jpg")

    def locate(self, camera, pixels, plane_z=None):
        result = self._call("POST", f"/api/camera/{camera}/locate", {"pixels": [list(map(float, p)) for p in pixels], "plane_z": plane_z})
        return [p["xyz"] for p in result["points"]]

    def find_color(self, camera, color, min_area=40, plane_z=None):
        import cv2
        image = self.capture(camera)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = np.zeros(hsv.shape[:2], np.uint8)
        for low, high in COLORS[color]:
            mask |= cv2.inRange(hsv, np.array(low), np.array(high))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
        found = [(int(stats[i, cv2.CC_STAT_AREA]), centers[i]) for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= min_area]
        found.sort(key=lambda item: -item[0])
        if not found:
            return []
        points = self.locate(camera, [c for _, c in found], plane_z)
        return [{"pixel": [round(float(c[0])), round(float(c[1]))], "area": a, "xyz": p} for (a, c), p in zip(found, points)]

    def wait(self, seconds):
        time.sleep(float(seconds))


def run_code(code, timeout_s=120.0, url=None):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(textwrap.dedent(code))
        path = handle.name
    env = dict(os.environ, REBOT_URL=url or DEFAULT_URL, PYTHONUNBUFFERED="1")
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, "-m", "rebot_teleop.agent", "--run", path], capture_output=True, text=True, timeout=timeout_s, env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        output = (result.stdout + result.stderr)[-8000:]
        ok = result.returncode == 0
    except subprocess.TimeoutExpired as error:
        output = ((error.stdout or b"").decode(errors="ignore") if isinstance(error.stdout, bytes) else (error.stdout or ""))[-6000:] + f"\nTimed out after {timeout_s:.0f} s; the arms were told to hold"
        ok = False
    finally:
        os.unlink(path)
    if not ok:
        try:
            Robot(url).stop()
        except RobotError:
            pass
    return {"ok": ok, "output": output.strip() or ("Done" if ok else "Failed"), "seconds": round(time.monotonic() - started, 1)}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        robot = Robot()
        scope = {"robot": robot, "np": np, "math": math, "time": time, "RobotError": RobotError, "__name__": "__policy__"}
        source = open(sys.argv[2]).read()
        exec(compile(source, "policy.py", "exec"), scope)
        return
    print(API)


if __name__ == "__main__":
    main()
