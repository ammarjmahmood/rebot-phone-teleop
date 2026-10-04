import asyncio
import fnmatch
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
import psutil
import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from rebot_teleop.auth import Auth
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


def create_app(data_dir: Path, env_path: Path, node_factory=None, hebi_source=None):
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
    tunnel = CloudflareTunnel(port) if os.environ.get("REBOT_TELEOP_CLOUDFLARE") == "1" else None
    if tunnel:
        patterns.append("*.trycloudflare.com")

    def host_allowed(hostname):
        return hostname in allowed or any(fnmatch.fnmatch(hostname or "", pattern) for pattern in patterns)

    @asynccontextmanager
    async def lifespan(app):
        node = node_factory() if node_factory else TeleopNode(data_dir, os.environ.get("REBOT_TELEOP_CAN", "can0"))
        node.controllers = {
            "app": TeleopController(node, "app", PushSource(), "align"),
            "quest": TeleopController(node, "quest", PushSource(), "webxr"),
            "hebi": TeleopController(node, "hebi", hebi_source or HebiSource(), "calibrated"),
        }
        app.state.node = node
        await node.start()
        if tunnel:
            tunnel.start()
        yield
        if tunnel:
            tunnel.stop()
        await node.close()

    app = FastAPI(title="reBot teleop", lifespan=lifespan)

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

    def node(request):
        return request.app.state.node

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
        return {**node(request).status(), "principal": principal.model_dump()}

    @app.post("/api/power")
    async def power(request: Request):
        device(request)
        await node(request).power_on(bool((await request.json()).get("home")))
        return node(request).status()

    @app.post("/api/home")
    async def home(request: Request):
        principal = actor(request, "operator")
        current = node(request)
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
        current = node(request)
        for controller in current.controllers.values():
            await controller.stop("Stop pressed", keep_source=True)
        await current.stop("Stop pressed", latch=True)
        return {"status": "stopped"}

    @app.post("/api/reset")
    async def reset(request: Request):
        device(request)
        node(request).reset()
        return node(request).status()

    @app.post("/api/release")
    async def release(request: Request):
        device(request)
        await node(request).release_torque()
        return node(request).status()

    @app.post("/api/gripper-range")
    async def gripper_range(request: Request):
        device(request)
        return node(request).save_gripper((await request.json()).get("which"))

    @app.post("/api/remote")
    async def remote(request: Request):
        device(request)
        enabled = bool((await request.json()).get("enabled"))
        current = node(request)
        current.remote_until = time.monotonic() + 900 if enabled else 0
        if not enabled:
            for controller in current.controllers.values():
                await controller.stop("Phone motion revoked", keep_source=True)
        return {"enabled": enabled}

    @app.post("/api/pair")
    async def pair(request: Request):
        device(request)
        code = auth.pair("operator")
        addresses = []
        for name, address in hosts:
            url = f"http://{address}:{port}"
            addresses.append({"interface": name, "url": url, "quest": f"https://{address}:{https_port}/quest", "qr": segno.make(url, error="m").svg_data_uri(scale=5, border=2)})
        if tunnel and tunnel.url:
            addresses.insert(0, {"interface": "Cloudflare", "url": tunnel.url, "quest": tunnel.url + "/quest", "qr": segno.make(tunnel.url, error="m").svg_data_uri(scale=5, border=2)})
        return {"code": code, "expires_in": 300, "addresses": addresses, "lan": lan, "cloudflare": tunnel.status if tunnel else None}

    def controller(request, name):
        return node(request).controllers[name]

    @app.post("/api/hebi/{action}")
    async def hebi(action: str, request: Request):
        principal = device(request)
        body = await request.json()
        current = controller(request, "hebi")
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
            node(request).settings.set("hebi_addresses", parts)
        elif action == "rotation":
            current.use_rotation = bool(body.get("on"))
        else:
            raise HTTPException(404)
        return current.status()

    @app.post("/api/quest/{action}")
    async def quest(action: str, request: Request):
        principal = actor(request, "operator")
        body = await request.json()
        current = controller(request, "quest")
        if action == "start":
            current.set_view(body.get("view", current.view))
            current.use_rotation = bool(body.get("rotation", True))
            await current.start(principal)
            return current.status()
        if current.owner not in {None, principal.id}:
            raise ValueError("Another device has control")
        if action == "stop":
            await current.stop("Left VR")
            return current.status()
        if action == "pose":
            if not current.active:
                raise ValueError("Start VR control first")
            inputs = {str(k)[:4]: float(v) for k, v in dict(body.get("inputs", {})).items()}
            current.source.push(body["position"], body["quaternion_wxyz"], {k: (int(v) if k.startswith("b") else v) for k, v in inputs.items()})
            return {"note": current.note, "mode": current.mode}
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
        current_node = websocket.app.state.node
        current = current_node.controllers["app"]
        await websocket.accept()
        message_note = {"text": None}

        async def report():
            while True:
                status = current.status()
                arm = current_node.arm
                await websocket.send_json({
                    "type": "status", "owner": current.owner == principal.id, "active": status["active"], "mode": status["mode"],
                    "note": message_note["text"] or status["note"] or current_node.fault, "calibrated": status["ready"], "tilt": current.use_rotation, "view": current.view,
                    "torque": arm.torque, "fault": arm.fault, "remote": principal.local or time.monotonic() < current_node.remote_until,
                    "joints": [round(float(v) * 57.29578, 1) for v in arm.q], "gripper": round(float(arm.measured[6]) * 57.29578, 1), "phase": "TELEOP" if current_node.owner else "READY",
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
                            if message.get("grip") is not None:
                                inputs["grip"] = float(message["grip"])
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
            if current.owner == principal.id:
                await current.stop("Phone app disconnected")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/quest")
    async def quest_page():
        return FileResponse(STATIC / "quest.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
