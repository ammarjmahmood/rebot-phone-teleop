import asyncio
import base64
import fnmatch
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse
import psutil
import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
import numpy as np
from rebot_teleop import policy
from rebot_teleop import tasks
from rebot_teleop.agent import Robot
from rebot_teleop import cap
from rebot_teleop.camera import SimCamera, SimView, UsbCamera, locate, optical_pose, sim_capture, usb_capture
from fastapi.staticfiles import StaticFiles
from rebot_teleop.auth import Auth, pairing_ttl
from rebot_teleop.controller import TeleopController
from rebot_teleop.models import Principal
from rebot_teleop.node import TeleopNode
from rebot_teleop.sources import HebiSource, PushSource


STATIC = Path(__file__).resolve().parent / "static"
BRIDGES = ("docker", "br-", "veth", "virbr", "lo")


def lan_addresses():
    found = []
    for name, entries in psutil.net_if_addrs().items():
        if name.startswith(BRIDGES):
            continue
        for entry in entries:
            if entry.family.name != "AF_INET":
                continue
            address = ipaddress.ip_address(entry.address)
            if address.is_loopback or address.is_link_local:
                continue
            found.append((address in ipaddress.ip_network("100.64.0.0/10"), name, entry.address))
    return [(name, address) for _, name, address in sorted(found)]


def route_label(interface):
    if interface == "Cloudflare":
        return "Cloudflare"
    if interface.startswith(("tailscale", "ts")):
        return "Tailscale"
    if interface.startswith(("wl", "wifi")):
        return "WiFi"
    if interface.startswith("l4tbr"):
        return "USB cable"
    if interface.startswith(("en", "eth")):
        return "Ethernet"
    return interface


def app_link(url, code):
    return "rebotteleop://pair?" + urlencode({"url": url, "code": code})


def tailscale_name():
    try:
        status = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout)
        return status["Self"]["DNSName"].rstrip(".") or None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


class CloudflareTunnel:
    def __init__(self, port):
        self.port = port
        self.process = None
        self.url = None
        self.status = "off"

    def start(self):
        binary = shutil.which("cloudflared") or str(Path.home() / ".local/bin/cloudflared")
        if not Path(binary).exists():
            self.status = "cloudflared is not installed; run scripts/setup_cloudflared.sh"
            return
        self.process = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.status = "starting"
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stderr:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if match and not self.url:
                self.url = match.group(0)
                self.status = "running"
        self.status = "stopped"

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()


class GistBookmark:
    def __init__(self, token, gist_id, settings_path):
        self.token = token
        self.gist_id = gist_id
        self.settings_path = settings_path
        self.status = "off"

    def _request(self, method, url, body):
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method=method, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "User-Agent": "rebot-teleop"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    def publish(self, url):
        content = f"# reBot teleop\n\nCurrent links, updated {time.strftime('%Y-%m-%d %H:%M')}:\n\n1. Meta Quest: [{url}/quest]({url}/quest)\n2. Control page from anywhere: [{url}]({url})\n\nPairing still needs a code from the arm computer.\n"
        files = {"rebot-teleop.md": {"content": content}}
        try:
            if self.gist_id:
                self._request("PATCH", f"https://api.github.com/gists/{self.gist_id}", {"files": files})
            else:
                created = self._request("POST", "https://api.github.com/gists", {"description": "reBot teleop links", "public": False, "files": files})
                self.gist_id = created["id"]
                self.settings_path.write_text(self.gist_id)
            self.status = f"https://gist.github.com/{self.gist_id}"
        except Exception as error:
            self.status = f"gist update failed: {error}"


DEFAULT_MOTION_WINDOW = 900


def motion_window():
    try:
        value = int(os.environ.get("REBOT_TELEOP_MOTION_WINDOW", DEFAULT_MOTION_WINDOW))
    except ValueError:
        return DEFAULT_MOTION_WINDOW
    return value if 60 <= value <= 14400 else DEFAULT_MOTION_WINDOW


def create_app(data_dir: Path, env_path: Path, arms=None, node_factory=None, hebi_source=None):
    auth = Auth(env_path)
    port = int(os.environ.get("REBOT_TELEOP_PORT", "8080"))
    https_port = int(os.environ.get("REBOT_TELEOP_HTTPS_PORT", "9443"))
    lan = os.environ.get("REBOT_TELEOP_LAN") == "1"
    hosts = lan_addresses() if lan else []
    extra = [h.strip() for h in (auth.values.get("REBOT_TELEOP_ALLOWED_HOSTS", "") + "," + os.environ.get("REBOT_TELEOP_ALLOWED_HOSTS", "")).split(",") if h.strip()]
    allowed = {"localhost", "127.0.0.1", "testserver", *[address for _, address in hosts], *[h for h in extra if "*" not in h]}
    patterns = [h for h in extra if "*" in h]
    if lan:
        name = tailscale_name()
        if name:
            allowed.add(name)
    policy_speed = float(np.radians(float(os.environ.get("REBOT_TELEOP_COMMAND_DEG_S", "30"))))
    tunnel = CloudflareTunnel(port) if os.environ.get("REBOT_TELEOP_CLOUDFLARE") == "1" else None
    if tunnel:
        patterns.append("*.trycloudflare.com")
    arms = arms or {"arm": os.environ.get("REBOT_TELEOP_CAN", "can0")}
    sim = None
    if os.environ.get("REBOT_TELEOP_SIM"):
        from rebot_teleop.sim import SimWorld
        sim = SimWorld(gui=False)
    sim_view = SimView(sim) if sim else None
    camera_devices = json.loads(os.environ.get("REBOT_TELEOP_CAMERAS", "{}") or "{}")
    cameras = {}
    gist_file = Path(data_dir) / "gist_id"
    token = auth.values.get("REBOT_TELEOP_GIST_TOKEN", "")
    gist = GistBookmark(token, gist_file.read_text().strip() if gist_file.exists() else auth.values.get("REBOT_TELEOP_GIST_ID", ""), gist_file) if tunnel and token else None

    fixed_table = json.loads(os.environ.get("REBOT_TELEOP_TABLE", "") or "null")
    camera_fov = float(os.environ.get("REBOT_TELEOP_CAMERA_FOV", "80"))
    captures = {}
    task_state = {"name": None, "seed": None}

    def host_allowed(hostname):
        return hostname in allowed or any(fnmatch.fnmatch(hostname or "", pattern) for pattern in patterns)

    def build(name, channel):
        folder = Path(data_dir) if len(arms) == 1 else Path(data_dir) / "arms" / name
        if node_factory:
            current = node_factory(name, channel)
        else:
            current = TeleopNode(folder, channel, name=name, controller_factory=sim.controller if sim else None)
            if sim:
                sim.add_arm(channel, current.kin.mount, current.home_q)
        if name in camera_devices:
            cameras[name] = UsbCamera(camera_devices[name])
        elif sim and not node_factory:
            cameras[name] = SimCamera(sim, channel)
        if fixed_table:
            current.set_obstacles([tasks.table_box(fixed_table)])
        current.name = name
        current.hand = name if name in {"left", "right"} else "right"
        current.controllers = {
            "app": TeleopController(current, "app", PushSource(), "align"),
            "quest": TeleopController(current, "quest", PushSource(), "webxr"),
            "hebi": TeleopController(current, "hebi", hebi_source or HebiSource(), "calibrated"),
        }
        return current

    async def publish_gist():
        for _ in range(120):
            if tunnel.url:
                await asyncio.to_thread(gist.publish, tunnel.url)
                return
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(app):
        app.state.nodes = {name: build(name, channel) for name, channel in arms.items()}
        for current in app.state.nodes.values():
            await current.start()
        if tunnel:
            tunnel.start()
            if gist:
                asyncio.create_task(publish_gist())
        yield
        if tunnel:
            tunnel.stop()
        for current in app.state.nodes.values():
            await current.close()
        for camera in cameras.values():
            camera.close()
        if sim:
            sim.close()

    app = FastAPI(title="reBot teleop", lifespan=lifespan)
    app.state.cap = {"running": False, "instruction": "", "model": "", "effort": "", "result": None, "saved": None, "events": [], "run": None}

    def local(request):
        return request.client.host in {"127.0.0.1", "::1", "testclient"} and request.url.hostname in {"127.0.0.1", "localhost", "testserver"} and not any(h in request.headers for h in ["forwarded", "x-forwarded-for", "x-forwarded-host"])

    def bearer(header):
        token = header.removeprefix("Bearer ").strip()
        if not token:
            return None
        if hmac.compare_digest(token, auth.values["REBOT_TELEOP_ADMIN_TOKEN"]):
            return Principal(id="api_owner", role="owner", local=False)
        return auth.resolve(token, False)

    def actor(request, role="viewer"):
        principal = auth.resolve(request.cookies.get("rebot_teleop", ""), local(request)) or bearer(request.headers.get("authorization", ""))
        if not principal:
            raise HTTPException(401, "Pair this device first")
        if {"viewer": 0, "operator": 1, "owner": 2}[principal.role] < {"viewer": 0, "operator": 1, "owner": 2}[role]:
            raise HTTPException(403, "This action requires a higher role")
        return principal

    def device(request):
        principal = actor(request, "owner")
        if not principal.local:
            raise HTTPException(403, "Do this at the arm computer")
        return principal

    def nodes(request):
        return request.app.state.nodes

    def pick(request, name):
        everything = nodes(request)
        if name is None and len(everything) == 1:
            return next(iter(everything.values()))
        if name not in everything:
            raise ValueError("Choose an arm: " + ", ".join(everything))
        return everything[name]

    @app.middleware("http")
    async def boundaries(request, call_next):
        if not host_allowed(request.url.hostname):
            return JSONResponse({"detail": "Host is not configured"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.url.netloc:
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/session")
    async def session(request: Request):
        principal = auth.resolve(request.cookies.get("rebot_teleop", ""), local(request))
        if not principal:
            if local(request):
                principal = Principal(id=uuid.uuid4().hex, role="owner", local=True)
            else:
                body = await request.json()
                token = body.get("token", "")
                if not isinstance(token, str) or len(token) > 200:
                    raise ValueError("Invalid pairing code")
                principal = auth.login(token, request.client.host)
        signed = auth.sign(principal)
        response = JSONResponse({**principal.model_dump(), "session": signed})
        response.set_cookie("rebot_teleop", signed, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=28800)
        return response

    @app.get("/api/state")
    async def state(request: Request):
        principal = actor(request)
        everything = nodes(request)
        first = next(iter(everything.values()))
        return {**first.status(), "arm": first.name, "arms": {name: current.status() for name, current in everything.items()}, "principal": principal.model_dump(), "gist": gist.status if gist else None, "sim": sim.snapshot() if sim else None, "cameras": {name: camera.status() for name, camera in cameras.items()}}

    @app.post("/api/sim/hand")
    async def sim_hand(request: Request):
        actor(request, "operator")
        if not sim:
            raise HTTPException(404)
        body = await body_of(request)
        current = pick(request, body.get("arm"))
        joints = body.get("joints_deg")
        gripper = body.get("gripper_deg")
        sim.set_hand(current.arm.channel, np.radians(joints) if joints is not None else None, float(np.radians(gripper)) if gripper is not None else None)
        return {"status": "moved"}

    @app.post("/api/handguide")
    async def handguide(request: Request):
        actor(request, "operator")
        body = await body_of(request)
        current = pick(request, body.get("arm"))
        await current.set_hand_guide(bool(body.get("on")))
        return current.status()

    @app.post("/api/command")
    async def command(request: Request):
        principal = actor(request, "operator")
        body = await body_of(request)
        text = body.get("text")
        everything = nodes(request)
        if isinstance(text, str) and text.strip().lower() in {"stop", "freeze", "halt"}:
            for current in everything.values():
                await current.stop("Stop command")
            return {"done": ["stopped"]}
        return await run_steps(principal, everything, policy.plan(text, views_of(everything)), policy_speed, "Command")

    def views_of(everything):
        return {name: policy.ArmView(name, current.kin, current.arm.q, current.home_q) for name, current in everything.items()}

    async def run_steps(principal, everything, steps, speed, reason):
        used = {name for step in steps for name in step}
        for name in used:
            current = everything[name]
            current.authorize(principal)
            current.require_ready()
            if any(controller.active for controller in current.controllers.values()):
                raise ValueError(f"Stop VR, phone or HEBI control on the {name} arm first")
        done = []
        for step in steps:
            results = await asyncio.gather(*(everything[name].move_to(move.q, move.width, speed, reason=reason) for name, move in step.items()))
            done.extend(move.note for move in step.values())
            if not all(results):
                return {"done": done, "interrupted": True}
        return {"done": done}

    @app.post("/api/move")
    async def move(request: Request):
        principal = actor(request, "operator")
        body = await body_of(request)
        everything = nodes(request)
        wanted = body.get("arms")
        if not isinstance(wanted, dict) or not wanted:
            raise ValueError("Send arms: {name: {joints_deg | tip_xyz | tip_delta, gripper_m}}")
        unknown = [name for name in wanted if name not in everything]
        if unknown:
            raise ValueError("Unknown arm " + ", ".join(map(str, unknown)) + "; arms are " + ", ".join(everything))
        views = views_of(everything)
        step = {name: policy.target(views[name], spec) for name, spec in wanted.items()}
        speed = policy_speed
        if body.get("speed_deg_s") is not None:
            speed = float(np.radians(np.clip(float(body["speed_deg_s"]), 2.0, float(np.degrees(policy_speed)) * 2)))
        if body.get("dry_run"):
            return {"ok": True, "plan": {name: {"joints_deg": np.degrees(move.q).round(1).tolist(), "tip_xyz": views[name].kin.pose(move.q)[0].round(4).tolist(), "note": move.note} for name, move in step.items()}}
        return await run_steps(principal, everything, [step], speed, "Agent move")

    async def capture_from(request, name):
        everything = nodes(request)
        if name == "overview":
            if not sim:
                raise HTTPException(404, "Only the simulator has an overview camera")
            shot = await asyncio.to_thread(sim_capture, sim, "overview", 85, view_args(request))
        elif name not in cameras:
            raise HTTPException(404, "No camera on that arm; cameras are " + ", ".join([*cameras, *(["overview"] if sim else [])]))
        elif sim and isinstance(cameras[name], SimCamera):
            shot = await asyncio.to_thread(sim_capture, sim, everything[name].arm.channel)
        else:
            current = everything[name]
            q = current.arm.q.copy()
            position, rotation = current.kin.frame_pose("wrist_camera", q)
            shot = None
            for _ in range(30):
                shot = await asyncio.to_thread(usb_capture, cameras[name], optical_pose(position, rotation), camera_fov)
                if shot:
                    break
                await asyncio.sleep(0.1)
        if not shot:
            raise HTTPException(503, "No picture yet from " + name)
        captures[name] = shot
        return shot

    def view_args(request):
        query = request.query_params
        return float(query.get("yaw", 120.0)), float(query.get("pitch", -15.0)), float(query.get("distance", 1.9)), float(query.get("height", 0.95))

    @app.get("/api/camera/{name}/capture")
    async def camera_capture(name: str, request: Request):
        actor(request)
        shot = await capture_from(request, name)
        return {**shot["info"], "world_from_camera": np.round(shot["world_from_camera"], 5).tolist(), "has_depth": shot["depth_m"] is not None, "image_jpeg_base64": base64.b64encode(shot["jpeg"]).decode()}

    @app.post("/api/camera/{name}/locate")
    async def camera_locate(name: str, request: Request):
        actor(request)
        body = await body_of(request)
        shot = captures.get(name) if body.get("use_last", True) else None
        if shot is None:
            shot = await capture_from(request, name)
        plane = body.get("plane_z")
        if plane is None and shot["depth_m"] is None:
            table = (sim.table if sim else None) or fixed_table
            plane = table["top"] if table else None
        return {"points": locate(shot, body.get("pixels", []), plane), "camera": name}

    @app.get("/api/sim/scene")
    async def sim_scene(request: Request):
        actor(request)
        if not sim:
            raise HTTPException(404)
        return {**sim.scene_snapshot(), "renderer": getattr(sim, "render_note", "")}

    @app.get("/api/sim/objects")
    async def sim_objects(request: Request):
        actor(request)
        if not sim:
            raise HTTPException(404, "Object positions only exist in the simulator; use the cameras on the real robot")
        return sim.object_states()

    @app.get("/api/sim/task")
    async def sim_task_status(request: Request):
        actor(request)
        if not sim:
            raise HTTPException(404, "Tasks run in the simulator")
        result = {"tasks": {name: {"text": spec["text"], "arms": spec["arms"]} for name, spec in tasks.TASKS.items()}, "task": task_state["name"], "seed": task_state["seed"]}
        if task_state["name"]:
            states = sim.object_states()
            ok, detail = tasks.check(task_state["name"], states)
            result.update({"text": tasks.TASKS[task_state["name"]]["text"], "success": ok, "detail": detail})
        return result

    @app.post("/api/sim/task")
    async def sim_task_reset(request: Request):
        principal = actor(request, "operator")
        if not sim:
            raise HTTPException(404, "Tasks run in the simulator")
        body = await body_of(request)
        name = body.get("name")
        everything = nodes(request)
        if name in (None, "", "none"):
            await asyncio.to_thread(sim.set_scene, {})
            for current in everything.values():
                current.set_obstacles([])
            task_state.update(name=None, seed=None)
            return {"task": None}
        spec = tasks.scene(name, int(body.get("seed", 0)))
        box = tasks.table_box(spec["table"])
        for current in everything.values():
            current.set_obstacles([box])
        if body.get("ready", True):
            ready = {arm: policy.Move(arm, np.radians(tasks.READY_DEG[arm]), policy.WIDTH_OPEN, f"{arm} ready") for arm in everything if arm in tasks.READY_DEG}
            await asyncio.to_thread(sim.set_scene, {"table": spec["table"]})
            moved = await run_steps(principal, everything, [ready], policy_speed, "Task reset")
            if moved.get("interrupted"):
                raise ValueError("Task reset was interrupted")
        states = await asyncio.to_thread(sim.set_scene, spec)
        task_state.update(name=name, seed=int(body.get("seed", 0)))
        await asyncio.sleep(0.5)
        return {"task": name, "seed": task_state["seed"], "text": tasks.TASKS[name]["text"], "arms": tasks.TASKS[name]["arms"], "scene": sim.object_states()}

    @app.get("/api/sim/view.jpg")
    async def sim_view_still(request: Request, yaw: float = 120.0, pitch: float = -15.0, distance: float = 1.9, height: float = 0.95):
        actor(request)
        if not sim:
            raise HTTPException(404, "Only the simulator has an overview camera")
        for _ in range(40):
            frame = await asyncio.to_thread(sim_view.frame, yaw, pitch, distance, height)
            if frame:
                return Response(frame, media_type="image/jpeg")
            await asyncio.sleep(0.05)
        raise HTTPException(503, "The simulator view is not ready")

    @app.get("/api/sim/view.mjpg")
    async def sim_view_stream(request: Request, yaw: float = 120.0, pitch: float = -15.0, distance: float = 1.9, height: float = 0.95):
        actor(request)
        if not sim:
            raise HTTPException(404, "Only the simulator has an overview camera")

        async def frames():
            last = None
            while not await request.is_disconnected():
                frame = await asyncio.to_thread(sim_view.frame, yaw, pitch, distance, height)
                if frame and frame is not last:
                    last = frame
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/camera/{name}.jpg")
    async def camera_still(name: str, request: Request):
        actor(request)
        camera = cameras.get(name)
        if camera is None:
            raise HTTPException(404, "No camera on that arm")
        for _ in range(30):
            frame = await asyncio.to_thread(camera.frame)
            if frame:
                return Response(frame, media_type="image/jpeg")
            await asyncio.sleep(0.1)
        raise HTTPException(503, camera.status()["note"])

    @app.get("/api/camera/{name}.mjpg")
    async def camera_stream(name: str, request: Request):
        actor(request)
        camera = cameras.get(name)
        if camera is None:
            raise HTTPException(404, "No camera on that arm")

        async def frames():
            last = None
            while not await request.is_disconnected():
                frame = await asyncio.to_thread(camera.frame)
                if frame and frame is not last:
                    last = frame
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                await asyncio.sleep(0.04)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/api/sim/temperature")
    async def sim_temperature(request: Request):
        device(request)
        if not sim:
            raise HTTPException(404)
        body = await body_of(request)
        current = pick(request, body.get("arm"))
        sim.set_temperature(current.arm.channel, int(body["joint"]) - 1, float(body["celsius"]))
        return sim.snapshot()

    async def body_of(request):
        try:
            return await request.json()
        except ValueError:
            return {}

    @app.post("/api/power")
    async def power(request: Request):
        device(request)
        body = await body_of(request)
        current = pick(request, body.get("arm"))
        await current.power_on(bool(body.get("home")))
        return current.status()

    @app.post("/api/home")
    async def home(request: Request):
        principal = actor(request, "operator")
        current = pick(request, (await body_of(request)).get("arm"))
        current.authorize(principal)
        active = next((c for c in current.controllers.values() if c.active), None)
        task = asyncio.create_task(current.home_with(active, principal) if active else current.home())
        await asyncio.sleep(0.05)
        if task.done() and task.exception():
            raise task.exception()
        return {"status": "homing"}

    @app.post("/api/stop")
    async def stop(request: Request):
        actor(request, "operator")
        for current in nodes(request).values():
            for controller in current.controllers.values():
                await controller.stop("Stop pressed", keep_source=True)
            await current.stop("Stop pressed", latch=True)
        return {"status": "stopped"}

    @app.post("/api/reset")
    async def reset(request: Request):
        device(request)
        name = (await body_of(request)).get("arm")
        for current in ([pick(request, name)] if name else nodes(request).values()):
            current.reset()
        return {"status": "reset"}

    @app.post("/api/rest")
    async def rest(request: Request):
        device(request)
        body = await body_of(request)
        current = pick(request, body.get("arm"))
        if body.get("save"):
            return current.save_rest()
        await current.rest()
        return current.status()

    @app.post("/api/release")
    async def release(request: Request):
        device(request)
        current = pick(request, (await body_of(request)).get("arm"))
        await current.release_torque()
        return current.status()

    @app.post("/api/gripper-range")
    async def gripper_range(request: Request):
        device(request)
        body = await body_of(request)
        return pick(request, body.get("arm")).save_gripper(body.get("which"))

    @app.post("/api/remote")
    async def remote(request: Request):
        device(request)
        enabled = bool((await body_of(request)).get("enabled"))
        for current in nodes(request).values():
            current.remote_until = time.monotonic() + motion_window() if enabled else 0
            if not enabled:
                for controller in current.controllers.values():
                    await controller.stop("Phone motion revoked", keep_source=True)
        return {"enabled": enabled}

    @app.post("/api/pair")
    async def pair(request: Request):
        device(request)
        code = auth.pair("operator")
        addresses = []
        routes = [(name, f"http://{address}:{port}", f"https://{address}:{https_port}/quest") for name, address in hosts]
        if tunnel and tunnel.url:
            routes.insert(0, ("Cloudflare", tunnel.url, tunnel.url + "/quest"))
        for name, url, quest in routes:
            link = app_link(url, code)
            addresses.append({"interface": name, "label": route_label(name), "url": url, "quest": quest, "app_link": link, "qr": segno.make(link, error="m").svg_data_uri(scale=5, border=2)})
        return {"code": code, "expires_in": pairing_ttl(), "addresses": addresses, "lan": lan, "cloudflare": tunnel.status if tunnel else None, "gist": gist.status if gist else None}

    @app.post("/api/hebi/{action}")
    async def hebi(action: str, request: Request):
        principal = device(request)
        body = await body_of(request)
        current = pick(request, body.get("arm")).controllers["hebi"]
        if action == "start":
            await current.start(principal)
        elif action == "stop":
            await current.stop("HEBI stopped")
        elif action == "calibrate":
            current.calibrate(body.get("step"))
        elif action == "address":
            parts = [part.strip() for part in str(body.get("address") or "").split(",") if part.strip()]
            for part in parts:
                ipaddress.ip_address(part)
            current.node.settings.set("hebi_addresses", parts)
        elif action == "rotation":
            current.use_rotation = bool(body.get("on"))
        else:
            raise HTTPException(404)
        return current.status()

    def quest_controllers(request):
        return {current.hand: current.controllers["quest"] for current in nodes(request).values()}

    @app.post("/api/quest/{action}")
    async def quest(action: str, request: Request):
        principal = actor(request, "operator")
        body = await body_of(request)
        controllers = quest_controllers(request)
        if action == "start":
            started = []
            for hand, current in controllers.items():
                current.set_view(body.get("view", current.view))
                current.use_rotation = bool(body.get("rotation", True))
                await current.start(principal)
                started.append(hand)
            return {"hands": started}
        if any(c.owner not in {None, principal.id} for c in controllers.values()):
            raise ValueError("Another device has control")
        if action == "stop":
            for current in controllers.values():
                await current.stop("Left VR")
            return {"status": "stopped"}
        if action == "pose":
            hands = body.get("hands") or {"right": {key: body[key] for key in ("position", "quaternion_wxyz", "inputs") if key in body}}
            if not any(c.active for c in controllers.values()):
                raise ValueError("Start VR control first")
            notes = {}
            for hand, sample in hands.items():
                current = controllers.get(hand)
                if current is None or not current.active or "position" not in sample:
                    continue
                inputs = {str(k)[:4]: float(v) for k, v in dict(sample.get("inputs", {})).items()}
                current.source.push(sample["position"], sample["quaternion_wxyz"], {k: (int(v) if k.startswith("b") else v) for k, v in inputs.items()})
                notes[hand] = current.note
            return {"notes": notes, "note": next((n for n in notes.values() if n), None), "hands": list(controllers)}
        raise HTTPException(404)

    @app.websocket("/ws/app")
    async def app_socket(websocket: WebSocket):
        if not host_allowed(websocket.url.hostname):
            await websocket.close(code=4403)
            return
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc != websocket.url.netloc:
            await websocket.close(code=4403)
            return
        principal = bearer(websocket.headers.get("authorization", "")) or auth.resolve(websocket.cookies.get("rebot_teleop", ""), False)
        if principal is None or principal.role == "viewer":
            await websocket.close(code=4401)
            return
        everything = websocket.app.state.nodes
        chosen = {"name": next(iter(everything))}
        await websocket.accept()
        message_note = {"text": None}

        def controller():
            return everything[chosen["name"]].controllers["app"]

        async def report():
            while True:
                current_node = everything[chosen["name"]]
                current = controller()
                status = current.status()
                arm = current_node.arm
                await websocket.send_json({
                    "type": "status", "owner": current.owner == principal.id, "active": status["active"], "mode": status["mode"],
                    "note": message_note["text"] or status["note"] or current_node.fault, "calibrated": status["ready"], "tilt": current.use_rotation, "view": current.view,
                    "torque": arm.torque, "fault": arm.fault, "remote": principal.local or time.monotonic() < current_node.remote_until,
                    "joints": [round(float(v) * 57.29578, 1) for v in arm.q], "gripper": round(float(arm.measured[6]) * 57.29578, 1), "phase": "TELEOP" if current_node.owner else "READY",
                    "arm": chosen["name"], "arms": list(everything),
                })
                message_note["text"] = None
                await asyncio.sleep(0.1)

        reporter = asyncio.create_task(report())
        last_grip = {"value": None}
        try:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type") if isinstance(message, dict) else None
                try:
                    if kind == "arm":
                        name = message.get("arm")
                        if name not in everything:
                            raise ValueError("Unknown arm")
                        if name != chosen["name"]:
                            previous = controller()
                            if previous.owner == principal.id:
                                await previous.stop("Switched arm", keep_source=True)
                            chosen["name"] = name
                            last_grip["value"] = None
                            message_note["text"] = f"Now controlling {name}"
                        continue
                    current = controller()
                    current_node = everything[chosen["name"]]
                    if kind == "pose":
                        mine = current.active and current.owner == principal.id
                        grip = message.get("grip")
                        grip_changed = grip is not None and last_grip["value"] is not None and abs(float(grip) - last_grip["value"]) > 0.01
                        last_grip["value"] = None if grip is None else float(grip)
                        if (message.get("move") or grip_changed) and not mine:
                            await current.start(principal)
                            mine = True
                        if mine:
                            inputs = {"b1": 1 if message.get("move") else 0}
                            if grip is not None:
                                inputs["grip"] = float(grip)
                            current.source.push([float(v) for v in message["p"]][:3], [float(v) for v in message["q"]][:4], inputs)
                    elif kind == "start":
                        current.use_rotation = bool(message.get("tilt", True))
                        current.set_view(message.get("view", current.view))
                        await current.start(principal)
                    elif kind == "view":
                        current.set_view(message.get("view"))
                    elif kind == "tilt":
                        current.use_rotation = bool(message.get("on"))
                    elif kind in {"align", "forward"}:
                        current.align()
                        message_note["text"] = "Aligned"
                    elif kind == "home":
                        asyncio.create_task(current_node.home_with(current, principal) if current.active else current_node.home())
                    elif kind == "stop":
                        if current.owner == principal.id:
                            await current.stop("Phone app stopped")
                except (KeyError, TypeError, ValueError) as error:
                    message_note["text"] = str(error) or "Request rejected"
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reporter.cancel()
            for current_node in everything.values():
                current = current_node.controllers["app"]
                if current.owner == principal.id:
                    await current.stop("Phone app disconnected")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/quest")
    async def quest_page():
        return FileResponse(STATIC / "quest.html")

    @app.get("/cap")
    async def cap_page():
        return FileResponse(STATIC / "cap.html")

    def cap_view(after=0):
        state = app.state.cap
        return {
            "running": state["running"], "instruction": state["instruction"], "model": state["model"], "effort": state["effort"],
            "result": state["result"], "saved": state["saved"], "events": state["events"][after:], "next": len(state["events"]),
            "key": bool(shutil.which("claude") or (Path.home() / ".local" / "bin" / "claude").exists()),
            "default_model": cap.DEFAULT_MODEL, "efforts": cap.EFFORTS,
        }

    @app.get("/api/cap")
    async def cap_status(request: Request, after: int = 0):
        device(request)
        return cap_view(max(0, after))

    @app.post("/api/cap")
    async def cap_start(request: Request):
        device(request)
        body = await body_of(request)
        state = app.state.cap
        if state["running"]:
            raise ValueError("A policy is already running; stop it first")
        instruction = str(body.get("instruction", "")).strip()
        model = str(body.get("model") or cap.DEFAULT_MODEL).strip()
        effort = str(body.get("effort") or "high")
        if not instruction or len(instruction) > 2000:
            raise ValueError("Type what the arms should do")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,80}", model):
            raise ValueError("That model name does not look right")
        if effort not in cap.EFFORTS:
            raise ValueError("Choose an effort: " + ", ".join(cap.EFFORTS))
        if not (shutil.which("claude") or (Path.home() / ".local" / "bin" / "claude").exists()):
            raise ValueError("The claude CLI is not installed on this computer")
        events = []
        state.update(running=True, instruction=instruction, model=model, effort=effort, result=None, saved=None, events=events)

        def emit(kind, text, **extra):
            events.append({"kind": kind, "text": text, "at": round(time.time(), 1), **extra})

        def work():
            run = None
            try:
                robot = Robot(f"http://127.0.0.1:{port}")
                robot.ensure_ready()
                run = cap.CliRun(f"http://127.0.0.1:{port}", instruction, model, effort, emit=emit)
                state["run"] = run
                ok = run.run()
                saved = run.save()
                state.update(result="done" if ok else "failed", saved=str(saved.relative_to(cap.ROOT)) if saved else None)
            except Exception as error:
                emit("error", f"{type(error).__name__}: {error}")
                state["result"] = "failed"
            finally:
                state.update(running=False, run=None)

        threading.Thread(target=work, name="code-as-policy", daemon=True).start()
        return cap_view()

    @app.post("/api/cap/stop")
    async def cap_stop(request: Request):
        device(request)
        run = app.state.cap.get("run")
        if run is not None:
            run.stop()
        for current in nodes(request).values():
            for controller in current.controllers.values():
                await controller.stop("Stop pressed", keep_source=True)
            await current.stop("Stop pressed", latch=True)
        return cap_view()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
