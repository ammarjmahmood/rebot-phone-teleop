import argparse
import asyncio
import faulthandler
import os
import signal
import uvicorn


def main():
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    parser = argparse.ArgumentParser(description="reBot server: dashboard, Quest and phone teleop, sequencer, cameras, table lift and the robot API for Claude")
    parser.add_argument("--sim", nargs="?", const="headless", help="Run with simulated arms (no CAN traffic)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("REBOT_PORT", 8081)))
    parser.add_argument("--https-port", type=int, default=int(os.environ.get("REBOT_HTTPS_PORT", 8443)))
    parser.add_argument("--local", action="store_true", help="Listen on this computer only")
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=IFACE")
    parser.add_argument("--lan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--camera", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--pairing-ttl", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--motion-window", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ["REBOT_PORT"] = str(args.port)
    os.environ["REBOT_HTTPS_PORT"] = str(args.https_port)
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from rebot_teleop import config as C
    C.HTTP_PORT, C.HTTPS_PORT = args.port, args.https_port
    for item in args.arm:
        name, _, iface = item.partition("=")
        if name and iface:
            C.DEFAULT_CAN[name.strip()] = iface.strip()
    from rebot_teleop.links import addresses
    from rebot_teleop.server import create_app
    from rebot_teleop.tls import ensure_certificate
    app = create_app(sim=bool(args.sim))
    host = "127.0.0.1" if args.local else "0.0.0.0"
    lan, tailnet = addresses()
    cert, key = ensure_certificate(C.DATA / "tls", lan + tailnet)
    configs = [
        uvicorn.Config(app, host=host, port=args.port, access_log=False, ws_ping_interval=5, ws_ping_timeout=10),
        uvicorn.Config(app, host=host, port=args.https_port, access_log=False, lifespan="off", ssl_certfile=str(cert), ssl_keyfile=str(key), ws_ping_interval=5, ws_ping_timeout=10),
    ]
    print(f"reBot dashboard http://127.0.0.1:{args.port}/  Quest https://{(lan + tailnet + ['127.0.0.1'])[0]}:{args.https_port}/quest" + ("  (simulated arms)" if args.sim else ""), flush=True)

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
