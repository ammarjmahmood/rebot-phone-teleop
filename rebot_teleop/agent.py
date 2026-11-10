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
robot is connected to the reBot teleop server. Units: metres, degrees. Read docs/ROBOT_GUIDE.md for the full picture.
World frame: x forward toward the eyes, y to the robot's left, z up, origin on the floor at the foot of the post.
Arms are "left" (y > 0) and "right" (y < 0). Gripper width 0 (closed) to 0.05 m (open).

State and motion
robot.state()                            per arm: joints_deg, tip_xyz, tip_rotation, gripper_deg, ready, fault
robot.tip(arm) / robot.joints(arm)       gripper tip position in metres / six joint angles in degrees
robot.ready()                            both arms to the ready pose above the table, grippers open
robot.home()                             both arms to the hanging home pose
robot.move_tip(arm, xyz, rotation=None)  tip to a point; rotation is a 3x3 hand orientation, e.g. robot.top_down(arm, yaw)
robot.move_tip_by(arm, [dx, dy, dz])     shift the tip relative to now
robot.move_joints(arm, [j1..j6])         joint angles in degrees
robot.move(left={...}, right={...})      both arms together; spec keys joints_deg, tip_xyz, tip_delta, tip_rotation, gripper_m
robot.can_reach(arm, xyz, rotation=None) True or False without moving
robot.grip(arm, width_m)                 open or close
robot.top_down(arm, yaw_deg=0)           hand pointing straight down; fingers close along world x rotated by yaw
robot.pick(arm, xyz, yaw_deg=0)          open, come down from above to xyz (object centre), close, lift 10 cm
robot.place(arm, xyz, yaw_deg=0)         carry above xyz, lower until the held object's centre is at xyz, open, rise
robot.say("raise left hand then wave")   plain word commands
robot.stop()                             stop everything

Seeing
robot.capture(camera)                    (image HxWx3 uint8 RGB, info) for "left", "right" wrist cameras, "torso" (a fixed camera
                                         on the real robot looking at both arms and the table, when fitted) or "overview" (sim)
                                         info has fx, fy, cx, cy, width, height, world_from_camera (4x4, OpenCV axes)
robot.locate(camera, [(u, v), ...], plane_z=None)
                                         world xyz for pixels of the last capture; depth in sim, else a plane at plane_z
                                         (defaults to the table top)
robot.find_color(image, "red")           blobs of red, green, blue or yellow, biggest first: uv, pixels, box
robot.find_objects(camera, "red", z_range=(0.66, 0.80))
                                         capture, find blobs and locate them in 3D, keeping those in the height range;
                                         parts of the robot can match a colour, so filter by height
robot.snapshot(camera, path)             save a JPEG

Simulator only
robot.objects()                          ground truth object positions; not allowed in camera only evaluations
robot.task(name, seed)                   reset a benchmark task; returns its instructions
robot.task_status()                      success and detail for the current task

Every move is planned around the post, the head and the table, checked against joint limits and refused
before anything moves if it cannot be done. A refused move raises RobotError with the reason.
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
        arms = self._get("/api/state")["arms"]
        if all(arm.get("ready") for arm in arms.values()):
            return
        if self.sim:
            for name, arm in arms.items():
                if arm.get("fault") or arm.get("estopped"):
                    self.client.post("/api/reset", json={"arm": name})
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

    def ready(self):
        return self.say("get ready then open grippers")

    def top_down(self, arm, yaw_deg=0.0):
        side = 1.0 if self._arm(arm) == "left" else -1.0
        base = np.array([[0.0, -side, 0.0], [0.0, 0.0, side], [-1.0, 0.0, 0.0]])
        turn = math.radians(float(yaw_deg))
        spin = np.array([[math.cos(turn), -math.sin(turn), 0.0], [math.sin(turn), math.cos(turn), 0.0], [0.0, 0.0, 1.0]])
        return spin @ base

    def pick(self, arm, xyz, yaw_deg=0.0, approach=0.08, lift=0.10):
        xyz = np.asarray(xyz, dtype=float)
        rotation = self.top_down(arm, yaw_deg)
        self.move(**{arm: {"tip_xyz": xyz + [0, 0, approach], "tip_rotation": rotation, "gripper_m": 0.05}})
        self.move(**{arm: {"tip_xyz": xyz, "tip_rotation": rotation}}, speed_deg_s=15)
        self.grip(arm, 0.0)
        self.wait(0.4)
        return self.move(**{arm: {"tip_xyz": xyz + [0, 0, lift], "tip_rotation": rotation}}, speed_deg_s=15)

    def place(self, arm, xyz, yaw_deg=0.0, approach=0.08):
        xyz = np.asarray(xyz, dtype=float)
        rotation = self.top_down(arm, yaw_deg)
        self.move(**{arm: {"tip_xyz": xyz + [0, 0, approach], "tip_rotation": rotation}})
        self.move(**{arm: {"tip_xyz": xyz, "tip_rotation": rotation}}, speed_deg_s=15)
        self.grip(arm, 0.05)
        self.wait(0.3)
        return self.move(**{arm: {"tip_xyz": xyz + [0, 0, approach], "tip_rotation": rotation}})

    def capture(self, camera):
        from PIL import Image
        import base64
        data = self._get(f"/api/camera/{camera}/capture")
        image = np.array(Image.open(io.BytesIO(base64.b64decode(data.pop("image_jpeg_base64")))).convert("RGB"))
        data["world_from_camera"] = np.array(data["world_from_camera"])
        return image, data

    def locate(self, camera, pixels, plane_z=None):
        body = {"pixels": [[float(u), float(v)] for u, v in pixels]}
        if plane_z is not None:
            body["plane_z"] = float(plane_z)
        return [point["xyz"] for point in self._post(f"/api/camera/{camera}/locate", body)["points"]]

    @staticmethod
    def find_color(image, color, min_pixels=12):
        rgb = np.asarray(image, dtype=float)
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        masks = {
            "red": (r > 120) & (r > 1.8 * g) & (r > 1.8 * b),
            "green": (g > 90) & (g > 1.4 * r) & (g > 1.4 * b),
            "blue": (b > 90) & (b > 1.5 * r) & (b > 1.2 * g),
            "yellow": (r > 140) & (g > 120) & (b < 0.5 * g),
        }
        if color not in masks:
            raise RobotError(f"Colours are {', '.join(masks)}")
        from scipy import ndimage
        labels, count = ndimage.label(masks[color])
        blobs = []
        for index in range(1, count + 1):
            rows, cols = np.nonzero(labels == index)
            if rows.size >= min_pixels:
                blobs.append({"uv": (float(cols.mean()), float(rows.mean())), "pixels": int(rows.size), "box": (int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max()))})
        return sorted(blobs, key=lambda blob: -blob["pixels"])

    def find_objects(self, camera, color, z_range=None):
        image, _ = self.capture(camera)
        blobs = self.find_color(image, color)
        if not blobs:
            return []
        points = self.locate(camera, [blob["uv"] for blob in blobs])
        found = []
        for blob, xyz in zip(blobs, points):
            if z_range is None or z_range[0] <= xyz[2] <= z_range[1]:
                found.append({**blob, "xyz": xyz})
        return found

    def objects(self):
        if os.environ.get("REBOT_HIDE_OBJECTS") == "1":
            raise RobotError("Ground truth objects are hidden in this evaluation; find objects with robot.capture and robot.locate")
        return self._get("/api/sim/objects")

    def task(self, name, seed=0):
        self.ensure_ready()
        return self._post("/api/sim/task", {"name": name, "seed": int(seed)})

    def task_status(self):
        return self._get("/api/sim/task")

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
