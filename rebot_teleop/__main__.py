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
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
