import argparse
import asyncio
import faulthandler
import os
import signal
from pathlib import Path
import uvicorn


def detect_can():
    base = Path("/sys/class/net")
    names = []
    for path in sorted(base.iterdir()) if base.exists() else []:
        try:
            if (path / "type").read_text().strip() == "280":
                names.append(path.name)
        except OSError:
            continue
    for name in names:
        driver = base / name / "device" / "driver"
        if driver.exists() and driver.resolve().name == "peak_usb":
            return name
    return names[0] if names else "can0"


def can_ports():
    base = Path("/sys/class/net")
    ports = {}
    for path in sorted(base.iterdir()) if base.exists() else []:
        try:
            if (path / "type").read_text().strip() != "280" or not (path / "device").exists():
                continue
        except OSError:
            continue
        ports[path.name] = (path / "device").resolve().name.split(":")[0]
    return ports


def pin_arms(arms, saved, ports):
    by_port = {port: iface for iface, port in ports.items()}
    resolved = dict(arms)
    notes = []
    for name, port in saved.items():
        if name not in resolved:
            continue
        iface = by_port.get(port)
        if iface is None:
            resolved[name] = f"missing-usb-{port}"
            notes.append(f"{name}: its CAN adapter on USB {port} is not plugged in; the arm stays offline instead of using another adapter")
        elif iface != resolved[name]:
            notes.append(f"{name}: using {iface} (USB {port}) instead of {resolved[name]}, because adapter names changed")
            resolved[name] = iface
    used = [iface for iface in resolved.values() if not iface.startswith("missing")]
    if len(used) != len(set(used)):
        raise ValueError("Two arms point at the same CAN adapter: " + ", ".join(f"{n}={i}" for n, i in resolved.items()))
    return resolved, notes


def describe_can():
    base = Path("/sys/class/net")
    rows = []
    for path in sorted(base.iterdir()) if base.exists() else []:
        try:
            if (path / "type").read_text().strip() != "280":
                continue
        except OSError:
            continue
        driver = (path / "device" / "driver")
        usb = (path / "device").resolve().name if (path / "device").exists() else "-"
        state = (path / "operstate").read_text().strip() if (path / "operstate").exists() else "?"
        rows.append(f"{path.name:8} driver {driver.resolve().name if driver.exists() else '-':10} usb {usb:14} {state}")
    return rows or ["No CAN interfaces found"]


def open_dashboard(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("Simulation view:", url, flush=True)


def main():
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    parser = argparse.ArgumentParser(description="Phone, HEBI and Quest teleoperation for the Seeed reBot B601 RS arm")
    parser.add_argument("--can", default="auto", help="SocketCAN interface connected to the arm; auto picks the PEAK USB adapter")
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=IFACE", help="Add an arm, for example --arm left=can1 --arm right=can2; names left and right map to the Quest hands")
    parser.add_argument("--list-can", action="store_true", help="List CAN interfaces with their driver and USB port, then exit")
    parser.add_argument("--sim", nargs="?", const="gui", choices=["gui", "headless"], help="Drive simulated arms in PyBullet instead of the CAN bus; the same IK, gravity compensation, safety checks and Quest page run unchanged")
    parser.add_argument("--mount", action="append", default=[], metavar="NAME=X,Y,Z,ROLL,PITCH,YAW", help="Where an arm's base sits on the robot in metres and degrees, overriding the saved mount; left and right default to the side mounted post assembly")
    parser.add_argument("--camera-pose", action="append", default=[], metavar="NAME=X,Y,Z,LOOK_X,LOOK_Y,LOOK_Z", help="Where a fixed camera sits and the point it looks at, in metres in the world frame, so locate works for it")
    parser.add_argument("--camera", action="append", default=[], metavar="NAME=DEVICE", help="USB camera: an arm name for its wrist camera, for example --camera left=/dev/video0, or any other name for a fixed camera such as --camera torso=/dev/video2, or torso=folder:/dev/shm/rebot-torso for frames written by a camera bridge such as scripts/hp60c_bridge.py; in --sim each arm gets a rendered wrist camera")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--https-port", type=int, default=9443, help="HTTPS port for the Quest page")
    parser.add_argument("--lan", action="store_true", help="Listen on the network so phones and headsets can connect")
    parser.add_argument("--cloudflare", action="store_true", help="Also publish an https address through a free Cloudflare quick tunnel")
    parser.add_argument("--allow-host", action="append", default=[], help="Extra hostname the server may be reached by; wildcards like *.example.com work")
    parser.add_argument("--pairing-ttl", type=int, default=300, metavar="SECONDS", help="How long a pairing code stays valid, 30 to 86400 seconds; codes are single use and limited to five attempts a minute")
    parser.add_argument("--motion-window", type=int, default=900, metavar="SECONDS", help="How long Allow phone and headset motion lasts once ticked, 60 to 14400 seconds")
    parser.add_argument("--data", default=str(Path.home() / ".rebot-teleop"), help="Folder for settings, secrets and the local certificate")
    parser.add_argument("--can-port", action="append", default=[], metavar="NAME=USBPORT", help="Tie an arm to the USB port of its CAN adapter, for example --can-port left=1-2.1.1.4; saved in the data folder so CAN names that change on replug never swap the arms")
    args = parser.parse_args()
    import json
    port_file = Path(args.data) / "can_ports.json"
    saved_ports = json.loads(port_file.read_text()) if port_file.exists() else {}
    if args.can_port:
        for item in args.can_port:
            name, _, port = item.partition("=")
            if not name or not port:
                parser.error("--can-port needs NAME=USBPORT, for example left=1-2.1.1.4")
            saved_ports[name.strip()] = port.strip()
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(json.dumps(saved_ports, indent=1))
        print("Saved arm to USB port map in", port_file, saved_ports)
    if args.list_can:
        print("\n".join(describe_can()))
        ports = can_ports()
        for name, port in saved_ports.items():
            iface = next((i for i, p in ports.items() if p == port), None)
            print(f"{name:8} pinned to USB {port}: {iface or 'adapter not plugged in'}")
        return
    arms = {}
    for item in args.arm:
        name, _, iface = item.partition("=")
        if not name or not iface:
            parser.error("--arm needs NAME=IFACE, for example left=can1")
        arms[name.strip()] = iface.strip()
    if saved_ports and arms and not args.sim:
        try:
            arms, notes = pin_arms(arms, saved_ports, can_ports())
        except ValueError as error:
            parser.error(str(error))
        for note in notes:
            print(note)
        os.environ["REBOT_TELEOP_CAN_PORTS"] = json.dumps({name: port for name, port in saved_ports.items() if name in arms})
    mounts = {}
    for item in args.mount:
        name, _, values = item.partition("=")
        numbers = [float(v) for v in values.split(",")] if values else []
        if not name or len(numbers) != 6:
            parser.error("--mount needs NAME=X,Y,Z,ROLL,PITCH,YAW")
        mounts[name.strip()] = {"xyz": numbers[:3], "rpy_deg": numbers[3:]}
    cameras = {}
    for item in args.camera:
        name, _, device = item.partition("=")
        if not name or not device:
            parser.error("--camera needs NAME=DEVICE, for example left=/dev/video0")
        cameras[name.strip()] = device.strip()
    if cameras:
        import json
        os.environ["REBOT_TELEOP_CAMERAS"] = json.dumps(cameras)
    if mounts:
        import json
        os.environ["REBOT_TELEOP_MOUNTS"] = json.dumps(mounts)
    poses = {}
    for item in args.camera_pose:
        name, _, values = item.partition("=")
        numbers = [float(v) for v in values.split(",")] if values else []
        if not name or len(numbers) != 6:
            parser.error("--camera-pose needs NAME=X,Y,Z,LOOK_X,LOOK_Y,LOOK_Z")
        poses[name.strip()] = {"xyz": numbers[:3], "look_at": numbers[3:]}
    if poses:
        import json
        os.environ["REBOT_TELEOP_CAMERA_POSES"] = json.dumps(poses)
    if args.sim:
        os.environ["REBOT_TELEOP_SIM"] = args.sim
        if not arms:
            arms = {"left": "sim-left", "right": "sim-right"}
        print("Simulation:", args.sim, "(no CAN traffic, no motors move)")
    data = Path(args.data)
    os.environ["REBOT_TELEOP_CAN"] = "sim" if args.sim else detect_can() if args.can == "auto" else args.can
    print("Using CAN interface", os.environ["REBOT_TELEOP_CAN"])
    if not 30 <= args.pairing_ttl <= 86400:
        parser.error("--pairing-ttl must be between 30 and 86400 seconds")
    if not 60 <= args.motion_window <= 14400:
        parser.error("--motion-window must be between 60 and 14400 seconds")
    os.environ["REBOT_TELEOP_PAIRING_TTL"] = str(args.pairing_ttl)
    os.environ["REBOT_TELEOP_MOTION_WINDOW"] = str(args.motion_window)
    os.environ["REBOT_TELEOP_PORT"] = str(args.port)
    os.environ["REBOT_TELEOP_HTTPS_PORT"] = str(args.https_port)
    if args.cloudflare:
        os.environ["REBOT_TELEOP_CLOUDFLARE"] = "1"
    if args.allow_host:
        os.environ["REBOT_TELEOP_ALLOWED_HOSTS"] = ",".join(args.allow_host)
    host = "127.0.0.1"
    if args.lan:
        host = "0.0.0.0"
        os.environ["REBOT_TELEOP_LAN"] = "1"
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from rebot_teleop.server import create_app, lan_addresses
    if arms:
        print("Arms:", ", ".join(f"{name} on {iface}" for name, iface in arms.items()))
    app = create_app(data, data / ".env", arms=arms or None)
    configs = [uvicorn.Config(app, host=host, port=args.port, proxy_headers=False, access_log=False)]
    if args.lan:
        from rebot_teleop.tls import ensure_certificate
        cert, key = ensure_certificate(data / "tls", [address for _, address in lan_addresses()])
        configs.append(uvicorn.Config(app, host=host, port=args.https_port, proxy_headers=False, access_log=False, lifespan="off", ssl_certfile=str(cert), ssl_keyfile=str(key)))

    async def serve():
        servers = [uvicorn.Server(config) for config in configs]
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        if args.sim == "gui":
            asyncio.get_running_loop().call_later(4.0, open_dashboard, f"http://127.0.0.1:{args.port}/")
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
