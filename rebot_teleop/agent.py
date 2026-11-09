import argparse
import contextlib
import io
import math
import os
import sys
import time
import traceback
from pathlib import Path
import httpx
import numpy as np

DEFAULT_URL = "http://127.0.0.1:8080"
GUIDE = """\
robot is connected to the reBot teleop server. Units: metres, degrees.
World frame: x forward toward the eyes, y to the robot's left, z up, origin at the foot of the post.
Arms are named "left" and "right". Joints J1 to J6, gripper width 0 (closed) to 0.05 m (open).

robot.state()                          dict per arm: joints_deg, tip_xyz, tip_rotation, gripper_deg, ready, fault
robot.joints(arm)                      six joint angles in degrees
robot.tip(arm)                         gripper tip position [x, y, z] in metres
robot.move_joints(arm, [j1..j6])       go to joint angles
robot.move_tip(arm, [x, y, z])         put the tip at a point, keeping the hand orientation
robot.move_tip_by(arm, [dx, dy, dz])   shift the tip relative to where it is now
robot.move(left={...}, right={...})    move both arms together; each spec takes joints_deg, tip_xyz,
                                       tip_delta, tip_rotation (3x3) and gripper_m
robot.can_reach(arm, [x, y, z])        True or False, without moving
robot.grip(arm, width_m)               open or close the gripper
robot.say("raise left hand then wave") run plain words through the command parser
robot.home()                           both arms back to their hanging home pose
robot.stop()                           stop everything
robot.snapshot(name, path)             save a JPEG from "left" or "right" wrist camera, or "overview" in sim
robot.wait(seconds)

Every move is checked against joint limits, the floor and the keep out boxes around the post and head
before anything moves. A refused move raises RobotError with the reason; nothing moves in that case.
"""


class RobotError(RuntimeError):
    pass


class Robot:
    def __init__(self, url=None, code=None, token=None, allow_real=None, speed_deg_s=None):
        self.url = (url or os.environ.get("REBOT_URL") or DEFAULT_URL).rstrip("/")
        token = token or os.environ.get("REBOT_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=self.url, timeout=120, headers=headers)
        code = code if code is not None else os.environ.get("REBOT_CODE", "")
        self.session = os.environ.get("REBOT_SESSION", "")
        if self.session:
            self.client.cookies.set("rebot_teleop", self.session)
        elif not token:
            response = self.client.post("/api/session", json={"token": code} if code else {})
            if response.status_code >= 400:
                raise RobotError(f"Could not pair with {self.url}: {response.text}. Pass a pairing code from the robot computer")
            self.session = response.json().get("session", "")
        self.speed_deg_s = speed_deg_s
        snapshot = self._get("/api/state")
        self.sim = bool(snapshot.get("sim"))
        self.arms = list(snapshot["arms"])
        if allow_real is None:
            allow_real = os.environ.get("REBOT_ALLOW_REAL") == "1"
        if not self.sim and not allow_real:
            raise RobotError("This server drives real arms. Agents only move the simulator unless the person at the robot sets REBOT_ALLOW_REAL=1")

    def _get(self, path):
        response = self.client.get(path)
        if response.status_code >= 400:
            raise RobotError(_detail(response))
        return response.json()

    def _post(self, path, body):
        response = self.client.post(path, json=body)
        if response.status_code >= 400:
            raise RobotError(_detail(response))
        return response.json()

    def _arm(self, arm):
        if arm not in self.arms:
            raise RobotError(f"Unknown arm {arm!r}; arms are {', '.join(self.arms)}")
        return arm

    def state(self):
        full = self._get("/api/state")
        keys = ["joints_deg", "tip_xyz", "tip_rotation", "gripper_deg", "home_deg", "torque", "ready", "fault", "homing", "hand_guide", "temperatures"]
        arms = {name: {key: arm.get(key) for key in keys} for name, arm in full["arms"].items()}
        result = {"sim": self.sim, "arms": arms}
        if full.get("sim"):
            result["contacts"] = full["sim"].get("contacts", [])
        return result

    def joints(self, arm):
        return self.state()["arms"][self._arm(arm)]["joints_deg"]

    def tip(self, arm):
        return self.state()["arms"][self._arm(arm)]["tip_xyz"]

    def power_on(self):
        if not self.sim:
            raise RobotError("Power the real arms from the dashboard at the robot; agents may not")
        for name in self.arms:
            self._post("/api/power", {"arm": name, "home": False})
        time.sleep(1.0)
        return self.state()

    def ensure_ready(self):
        arms = self.state()["arms"]
        if all(arm.get("ready") for arm in arms.values()):
            return
        if self.sim:
            self.power_on()
            return
        raise RobotError("The real arms are not powered; the person at the robot must press Power on")

    def move(self, dry_run=False, speed_deg_s=None, **arms):
        specs = {}
        for name, spec in arms.items():
            if spec is None:
                continue
            specs[self._arm(name)] = {key: _plain(value) for key, value in spec.items() if value is not None}
        body = {"arms": specs, "dry_run": bool(dry_run)}
        speed = speed_deg_s or self.speed_deg_s
        if speed:
            body["speed_deg_s"] = float(speed)
        if not dry_run:
            self.ensure_ready()
        return self._post("/api/move", body)

    def move_joints(self, arm, joints_deg, **options):
        return self.move(**{arm: {"joints_deg": joints_deg}}, **options)

    def move_tip(self, arm, xyz, rotation=None, **options):
        return self.move(**{arm: {"tip_xyz": xyz, "tip_rotation": rotation}}, **options)

    def move_tip_by(self, arm, delta, **options):
        return self.move(**{arm: {"tip_delta": delta}}, **options)

    def can_reach(self, arm, xyz, rotation=None):
        try:
            self.move(dry_run=True, **{arm: {"tip_xyz": xyz, "tip_rotation": rotation}})
            return True
        except RobotError:
            return False

    def grip(self, arm, width_m):
        return self.move(**{arm: {"gripper_m": width_m}})

    def say(self, text):
        if text.strip().lower() not in {"stop", "halt", "freeze"}:
            self.ensure_ready()
        return self._post("/api/command", {"text": text})

    def home(self):
        return self.say("go home")

    def stop(self):
        return self._post("/api/command", {"text": "stop"})

    def snapshot(self, name, path=None):
        if name == "overview":
            response = self.client.get("/api/sim/view.jpg")
        else:
            response = self.client.get(f"/api/camera/{name}.jpg")
        if response.status_code >= 400:
            raise RobotError(_detail(response))
        if path:
            Path(path).write_bytes(response.content)
            return str(path)
        return response.content

    def wait(self, seconds):
        time.sleep(max(0.0, min(float(seconds), 60.0)))


def _plain(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _detail(response):
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    return f"{response.status_code}: {detail}"


def run_policy(code, robot, timeout=None):
    output = io.StringIO()
    scope = {"robot": robot, "np": np, "math": math, "time": time, "RobotError": RobotError, "__name__": "__policy__"}
    ok = True
    started = time.monotonic()
    with contextlib.redirect_stdout(output):
        try:
            exec(compile(code, "<policy>", "exec"), scope)
        except RobotError as error:
            ok = False
            print(f"RobotError: {error}")
        except BaseException:
            ok = False
            print(traceback.format_exc(limit=4))
            with contextlib.suppress(Exception):
                robot.stop()
    return ok, output.getvalue(), time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description="Run a code as policy script against the reBot teleop server; the script gets a ready made robot object")
    parser.add_argument("script", nargs="?", help="Python file to run, or - for stdin; leave out to print the robot API")
    parser.add_argument("--url", default=None, help=f"Teleop server, default $REBOT_URL or {DEFAULT_URL}")
    parser.add_argument("--code", default=None, help="Pairing code when the server is on another computer")
    parser.add_argument("--speed", type=float, default=None, help="Joint speed in degrees per second")
    args = parser.parse_args()
    if not args.script:
        print(GUIDE)
        return
    code = sys.stdin.read() if args.script == "-" else Path(args.script).read_text()
    try:
        robot = Robot(args.url, args.code, speed_deg_s=args.speed)
    except RobotError as error:
        sys.exit(str(error))
    ok, output, took = run_policy(code, robot)
    sys.stdout.write(output)
    print(f"[policy {'finished' if ok else 'failed'} in {took:.1f} s]")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
