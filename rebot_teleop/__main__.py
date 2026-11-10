import argparse
import asyncio
import os
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


def main():
    parser = argparse.ArgumentParser(description="Phone, HEBI and Quest teleoperation for the Seeed reBot B601 RS arm")
    parser.add_argument("--can", default="auto", help="SocketCAN interface connected to the arm; auto picks the PEAK USB adapter")
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=IFACE", help="Add an arm, for example --arm left=can1 --arm right=can2; names left and right map to the Quest hands")
    parser.add_argument("--mount", action="append", default=[], metavar="NAME=SPEC", help="How an arm is bolted down, for example --mount left=side_left@1.15; SPEC is upright, inverted, side_left, side_right or three rpy angles in degrees, optionally followed by @ and the height of the base above the floor in metres")
    parser.add_argument("--gravity-scale", type=float, default=1.0, help="Fraction of the computed gravity feedforward to apply, between 0 and 1; lower it for the first power on after changing how an arm is mounted")
    parser.add_argument("--pairing-ttl", type=int, default=300, metavar="SECONDS", help="How long a pairing code stays valid, 30 to 86400 seconds; the code is single use and guarded by five attempts a minute, so a longer window is reasonable when the headset is not next to the arm computer")
    parser.add_argument("--motion-window", type=int, default=900, metavar="SECONDS", help="How long the allow motion permission lasts once granted, 60 to 14400 seconds")
    parser.add_argument("--list-can", action="store_true", help="List CAN interfaces with their driver and USB port, then exit")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--https-port", type=int, default=9443, help="HTTPS port for the Quest page")
    parser.add_argument("--lan", action="store_true", help="Listen on the network so phones and headsets can connect")
    parser.add_argument("--cloudflare", action="store_true", help="Also publish an https address through a free Cloudflare quick tunnel")
    parser.add_argument("--allow-host", action="append", default=[], help="Extra hostname the server may be reached by; wildcards like *.example.com work")
    parser.add_argument("--data", default=str(Path.home() / ".rebot-teleop"), help="Folder for settings, secrets and the local certificate")
    args = parser.parse_args()
    if args.list_can:
        print("\n".join(describe_can()))
        return
    arms = {}
    for item in args.arm:
        name, _, iface = item.partition("=")
        if not name or not iface:
            parser.error("--arm needs NAME=IFACE, for example left=can1")
        arms[name.strip()] = iface.strip()
    from rebot_teleop.kinematics import parse_mount
    mounts = {}
    for item in args.mount:
        name, _, spec = item.partition("=")
        if not name or not spec:
            parser.error("--mount needs NAME=SPEC, for example left=side_left@1.15")
        try:
            parse_mount(spec.strip())
        except ValueError as error:
            parser.error(str(error))
        mounts[name.strip()] = spec.strip()
    unknown = sorted(set(mounts) - set(arms or {"arm"}))
    if unknown:
        parser.error("No such arm for --mount: " + ", ".join(unknown))
    if not 60 <= args.motion_window <= 14400:
        parser.error("--motion-window must be between 60 and 14400 seconds")
    os.environ["REBOT_TELEOP_MOTION_WINDOW"] = str(args.motion_window)
    if not 30 <= args.pairing_ttl <= 86400:
        parser.error("--pairing-ttl must be between 30 and 86400 seconds")
    os.environ["REBOT_TELEOP_PAIRING_TTL"] = str(args.pairing_ttl)
    if not 0.0 <= args.gravity_scale <= 1.0:
        parser.error("--gravity-scale must be between 0 and 1")
    data = Path(args.data)
    os.environ["REBOT_TELEOP_CAN"] = detect_can() if args.can == "auto" else args.can
    print("Using CAN interface", os.environ["REBOT_TELEOP_CAN"])
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
        print("Arms:", ", ".join(f"{name} on {iface}" + (f" mounted {mounts[name]}" if name in mounts else "") for name, iface in arms.items()))
    elif mounts:
        print("Mount:", mounts.get("arm", ""))
    app = create_app(data, data / ".env", arms=arms or None, mounts=mounts or None, gravity_scale=args.gravity_scale)
    configs = [uvicorn.Config(app, host=host, port=args.port, proxy_headers=False, access_log=False)]
    if args.lan:
        from rebot_teleop.tls import ensure_certificate
        cert, key = ensure_certificate(data / "tls", [address for _, address in lan_addresses()])
        configs.append(uvicorn.Config(app, host=host, port=args.https_port, proxy_headers=False, access_log=False, lifespan="off", ssl_certfile=str(cert), ssl_keyfile=str(key)))

    async def serve():
        servers = [uvicorn.Server(config) for config in configs]
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
