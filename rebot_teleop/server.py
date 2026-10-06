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
import urllib.request
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse
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
    tunnel = CloudflareTunnel(port) if os.environ.get("REBOT_TELEOP_CLOUDFLARE") == "1" else None
    if tunnel:
        patterns.append("*.trycloudflare.com")
    arms = arms or {"arm": os.environ.get("REBOT_TELEOP_CAN", "can0")}
    gist_file = Path(data_dir) / "gist_id"
    token = auth.values.get("REBOT_TELEOP_GIST_TOKEN", "")
    gist = GistBookmark(token, gist_file.read_text().strip() if gist_file.exists() else auth.values.get("REBOT_TELEOP_GIST_ID", ""), gist_file) if tunnel and token else None

    def host_allowed(hostname):
        return hostname in allowed or any(fnmatch.fnmatch(hostname or "", pattern) for pattern in patterns)

    def build(name, channel):
        folder = Path(data_dir) if len(arms) == 1 else Path(data_dir) / "arms" / name
        current = node_factory(name, channel) if node_factory else TeleopNode(folder, channel)
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
        return {**first.status(), "arm": first.name, "arms": {name: current.status() for name, current in everything.items()}, "principal": principal.model_dump(), "gist": gist.status if gist else None}

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
        routes = [(name, f"http://{address}:{port}", f"https://{address}:{https_port}/quest") for name, address in hosts]
        if tunnel and tunnel.url:
            routes.insert(0, ("Cloudflare", tunnel.url, tunnel.url + "/quest"))
        for name, url, quest in routes:
            link = app_link(url, code)
            addresses.append({"interface": name, "label": route_label(name), "url": url, "quest": quest, "app_link": link, "qr": segno.make(link, error="m").svg_data_uri(scale=5, border=2)})
        return {"code": code, "expires_in": 300, "addresses": addresses, "lan": lan, "cloudflare": tunnel.status if tunnel else None, "gist": gist.status if gist else None}

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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
