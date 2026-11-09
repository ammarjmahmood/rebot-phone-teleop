import argparse
import time
import httpx
import numpy as np
from rebot_teleop.kinematics import mount_placement
from rebot_teleop.sim import SimWorld


def connect(url, code):
    client = httpx.Client(base_url=url.rstrip("/"), timeout=10)
    response = client.post("/api/session", json={"token": code} if code else {})
    if response.status_code >= 400:
        raise SystemExit(f"Pairing failed: {response.text}; pass --code with a pairing code from the robot computer")
    return client


def main():
    parser = argparse.ArgumentParser(description="Open a PyBullet window that mirrors the joint angles of a running reBot teleop server, real or simulated")
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Address of the teleop server, for example the Jetson over Tailscale or the Cloudflare link")
    parser.add_argument("--code", default="", help="Pairing code from Show pairing code on the robot computer; not needed on the same machine")
    parser.add_argument("--rate", type=float, default=30.0)
    args = parser.parse_args()
    client = connect(args.url, args.code)
    state = client.get("/api/state").json()
    world = SimWorld(gui=True, heat=0.0)
    names = list(state["arms"])
    for name in names:
        arm = state["arms"][name]
        world.add_arm(name, mount_placement(**arm["mount"]), np.radians(arm["joints_deg"]))
    world.controller(names[0])
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
