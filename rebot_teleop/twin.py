import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import httpx
import numpy as np
from rebot_teleop.camera import SimView
from rebot_teleop.kinematics import mount_placement
from rebot_teleop.sim import SimWorld

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>reBot mirror</title><style>body{margin:0;background:#111;color:#ddd;font:14px system-ui}#v{width:100%;max-width:960px;display:block;margin:auto;touch-action:none;cursor:grab}
.bar{text-align:center;padding:8px}button{margin:2px;padding:6px 10px}#j{white-space:pre;font:12px monospace;text-align:center}</style></head><body>
<img id="v" draggable="false"><div class="bar"><button data-v="90,-10">Face</button><button data-v="180,-12">Left side</button><button data-v="0,-12">Right side</button>
<button data-v="270,-12">Back</button><button data-v="90,-80">Top</button><button data-z="0.8">Closer</button><button data-z="1.25">Farther</button></div><div id="j"></div>
<script>
const s={yaw:120,pitch:-15,d:1.9},v=document.getElementById('v');let drag=null,t;
function go(){clearTimeout(t);t=setTimeout(()=>v.src=`/view.mjpg?yaw=${s.yaw.toFixed(0)}&pitch=${s.pitch.toFixed(0)}&distance=${s.d.toFixed(2)}`,120)}
v.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY,yaw:s.yaw,pitch:s.pitch};v.setPointerCapture(e.pointerId)};
v.onpointermove=e=>{if(!drag)return;s.yaw=drag.yaw-(e.clientX-drag.x)*.5;s.pitch=Math.max(-85,Math.min(30,drag.pitch-(e.clientY-drag.y)*.4));go()};
v.onpointerup=v.onpointercancel=()=>drag=null;
document.onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.v)[s.yaw,s.pitch]=b.dataset.v.split(',').map(Number);if(b.dataset.z)s.d=Math.max(.5,Math.min(4,s.d*b.dataset.z));go()};
setInterval(async()=>{try{document.getElementById('j').textContent=await (await fetch('/joints')).text()}catch(e){}},500);go();
</script></body></html>"""


def serve(world, port, latest):
    view = SimView(world)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            query = {k: float(v[0]) for k, v in parse_qs(url.query).items() if k in ("yaw", "pitch", "distance")}
            if url.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/joints":
                body = latest.get("text", "").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/view.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                last = None
                try:
                    while True:
                        frame = view.frame(query.get("yaw", 120.0), query.get("pitch", -15.0), query.get("distance", 1.9), 0.95)
                        if frame and frame is not last:
                            last = frame
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                        time.sleep(0.04)
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def connect(url, code, wait=0.0):
    client = httpx.Client(base_url=url.rstrip("/"), timeout=10)
    deadline = time.monotonic() + wait
    while True:
        try:
            response = client.post("/api/session", json={"token": code} if code else {})
            break
        except httpx.HTTPError:
            if time.monotonic() > deadline:
                raise SystemExit(f"Cannot reach {url}")
            time.sleep(1.0)
    if response.status_code >= 400:
        raise SystemExit(f"Pairing failed: {response.text}; pass --code with a pairing code from the robot computer")
    return client


def main():
    parser = argparse.ArgumentParser(description="Open a PyBullet window that mirrors the joint angles of a running reBot teleop server, real or simulated")
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Address of the teleop server, for example the Jetson over Tailscale or the Cloudflare link")
    parser.add_argument("--code", default="", help="Pairing code from Show pairing code on the robot computer; not needed on the same machine")
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument("--wait", type=float, default=0.0, help="Seconds to keep trying while the server starts")
    parser.add_argument("--web", type=int, default=0, metavar="PORT", help="Show the mirror as a web page rendered on the GPU at http://this-computer:PORT instead of a PyBullet window, which can drop links of the second arm")
    args = parser.parse_args()
    client = connect(args.url, args.code, args.wait)
    state = client.get("/api/state").json()
    world = SimWorld(gui=not args.web, heat=0.0)
    latest = {}
    names = list(state["arms"])
    for name in names:
        arm = state["arms"][name]
        world.add_arm(name, mount_placement(**arm["mount"]), np.radians(arm["joints_deg"]))
    world.controller(names[0])
    if args.web:
        serve(world, args.web, latest)
        print(f"Mirror page: http://127.0.0.1:{args.web}/ (also reachable from other devices on this network or tailnet)", flush=True)
    print("Mirroring", ", ".join(names), "from", args.url, "(close the window or press Ctrl+C to stop)")
    shown = 0.0
    last_contacts = None
    try:
        while world.thread.is_alive():
            try:
                state = client.get("/api/state").json()
            except httpx.HTTPError as error:
                print("Lost the server:", error)
                time.sleep(1.0)
                continue
            for name in names:
                arm = state["arms"][name]
                world.set_hand(name, np.radians(arm["joints_deg"]), float(np.radians(arm["gripper_deg"])))
                with world.lock:
                    world.arms[name].temperature[:] = [t if t is not None else 25.0 for t in arm["temperatures"]]
            contacts = world.snapshot()["contacts"]
            if contacts != last_contacts and contacts:
                print("Touching in the model:", ", ".join(f"{c['arm']} link {c['link']} with {c['with']}" for c in contacts))
            last_contacts = contacts
            latest["text"] = "\n".join(f"{name:5s} " + ("  ".join(f"J{i + 1} {v:6.1f}" for i, v in enumerate(state["arms"][name]["joints_deg"])) if state["arms"][name].get("connected", True) else "offline") for name in names)
            if time.monotonic() - shown > 1.0:
                shown = time.monotonic()
                print("  ".join(f"{name} " + " ".join(f"{v:6.1f}" for v in state["arms"][name]["joints_deg"]) + ("  hand guide" if state["arms"][name].get("hand_guide") else "") for name in names), flush=True)
            time.sleep(1.0 / args.rate)
    except KeyboardInterrupt:
        pass
    finally:
        world.close()


if __name__ == "__main__":
    main()
