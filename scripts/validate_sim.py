import argparse
import json
import math
import sys
import time
import httpx
import numpy as np
import pinocchio as pin


XR_TO_ROBOT = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
MOVES = {
    "forward": np.array([0.0, 0.0, -1.0]),
    "back": np.array([0.0, 0.0, 1.0]),
    "left": np.array([-1.0, 0.0, 0.0]),
    "right": np.array([1.0, 0.0, 0.0]),
    "up": np.array([0.0, 1.0, 0.0]),
    "down": np.array([0.0, -1.0, 0.0]),
}
HAND_START = {"left": np.array([-0.2, 1.1, -0.3]), "right": np.array([0.2, 1.1, -0.3])}


class Session:
    def __init__(self, url):
        self.client = httpx.Client(base_url=url, timeout=20)
        self.client.post("/api/session", json={}).raise_for_status()

    def post(self, path, body=None, ok=True):
        response = self.client.post("/api/" + path, json=body or {})
        if ok and response.status_code >= 400:
            raise RuntimeError(f"{path}: {response.status_code} {response.text}")
        return response

    def state(self):
        return self.client.get("/api/state").json()


def quaternion(rotation):
    q = pin.Quaternion(rotation)
    return [q.w, q.x, q.y, q.z]


def stream(session, hands, seconds, rate=45.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        payload = {}
        for hand, (position, rotation, inputs) in hands.items():
            payload[hand] = {"position": [float(v) for v in position], "quaternion_wxyz": quaternion(rotation), "inputs": inputs}
        session.post("quest/pose", {"hands": payload})
        time.sleep(1.0 / rate)


def gesture(session, names, offset, turn=np.eye(3), duration=1.2):
    start = {hand: (HAND_START[hand], np.eye(3), {"b1": 0, "grip": 1.0}) for hand in names}
    stream(session, start, 0.3)
    held = {hand: (HAND_START[hand], np.eye(3), {"b1": 1, "grip": 1.0}) for hand in names}
    stream(session, held, 0.3)
    steps = 30
    for i in range(1, steps + 1):
        u = i / steps
        angle = pin.log3(turn) * u
        moving = {hand: (HAND_START[hand] + offset * u, pin.exp3(angle), {"b1": 1, "grip": 1.0}) for hand in names}
        stream(session, moving, duration / steps)
    final = {hand: (HAND_START[hand] + offset, turn, {"b1": 1, "grip": 1.0}) for hand in names}
    stream(session, final, 1.0)
    release = {hand: (HAND_START[hand] + offset, turn, {"b1": 0, "grip": 1.0}) for hand in names}
    stream(session, release, 0.5)


def tips(state):
    arms = state["sim"]["arms"]
    return {name: (np.array(arms[info["channel"]]["tip_xyz"]), np.array(arms[info["channel"]]["tip_rotation"])) for name, info in state["arms"].items()}


def check(results, name, passed, detail):
    results.append({"check": name, "passed": bool(passed), "detail": detail})
    print(("PASS " if passed else "FAIL ") + name + ": " + detail, flush=True)


def wait_until(session, condition, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = session.state()
        if condition(state):
            return state
        time.sleep(0.2)
    return session.state()


def main():
    parser = argparse.ArgumentParser(description="Drive the simulated arms through the real Quest endpoint and check that the grippers follow the hands")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--distance", type=float, default=0.06)
    parser.add_argument("--report", default="")
    parser.add_argument("--skip-thermal", action="store_true")
    args = parser.parse_args()
    session = Session(args.url)
    results = []
    state = session.state()
    if not state.get("sim"):
        sys.exit("This server is not running with --sim; refusing to drive real hardware")
    names = [name for name in state["arms"] if name in HAND_START]
    if not names:
        sys.exit("Run the server with arms named left and right")
    for name in names:
        session.post("power", {"arm": name, "home": False})
    state = wait_until(session, lambda s: all(s["arms"][n]["torque"] for n in names), 5)
    time.sleep(2.0)
    state = session.state()
    for name in names:
        arm = state["arms"][name]
        sag = max(abs(v) for v in arm["tracking_deg"])
        check(results, f"{name} holds its pose with gravity compensation", sag < 1.5, f"worst tracking error {sag:.2f} deg, gravity torques {arm['gravity_nm']} Nm")
    session.post("quest/start", {"view": "behind", "rotation": True})
    for step in (np.array([0.0, 0.15, -0.10]), np.array([0.0, 0.10, -0.12])):
        gesture(session, names, step, duration=2.5)
    time.sleep(1.0)
    for move, direction in MOVES.items():
        before = tips(session.state())
        gesture(session, names, direction * args.distance)
        time.sleep(0.8)
        state = session.state()
        after = tips(state)
        expected = XR_TO_ROBOT @ direction
        for name in names:
            delta = after[name][0] - before[name][0]
            size = float(np.linalg.norm(delta))
            cosine = float(delta @ expected / size) if size > 1e-6 else 0.0
            note = state["arms"][name]["controllers"]["quest"]["note"]
            check(results, f"{name} hand {move} moves gripper {move}", cosine > 0.9 and abs(size - args.distance) < 0.35 * args.distance, f"moved {size * 100:.1f} cm, direction match {cosine:.2f}, world delta {np.round(delta * 100, 1).tolist()} cm" + (f", note: {note}" if note else ""))
    for axis, label in ((np.array([0.0, 0.0, 1.0]), "roll"), (np.array([1.0, 0.0, 0.0]), "pitch")):
        before = tips(session.state())
        turn = pin.exp3(axis * math.radians(20))
        gesture(session, names, np.zeros(3), turn)
        time.sleep(0.8)
        after = tips(session.state())
        expected = XR_TO_ROBOT @ axis
        for name in names:
            change = pin.log3(after[name][1] @ before[name][1].T)
            angle = float(np.linalg.norm(change))
            cosine = float(change @ expected / angle) if angle > 1e-6 else 0.0
            check(results, f"{name} hand {label} turns gripper the same way", cosine > 0.85 and abs(math.degrees(angle) - 20) < 8, f"turned {math.degrees(angle):.1f} deg about world {np.round(change / max(angle, 1e-9), 2).tolist()}, expected {expected.tolist()}")
    hands = {hand: (HAND_START[hand], np.eye(3), {"b1": 0, "grip": 0.0}) for hand in names}
    stream(session, hands, 3.0)
    state = session.state()
    for name in names:
        check(results, f"{name} trigger closes the gripper", state["arms"][name]["gripper_deg"] < 20, f"gripper at {state['arms'][name]['gripper_deg']} deg")
    session.post("quest/stop")
    state = session.state()
    contacts = state["sim"]["contacts"]
    check(results, "no arm touched the post, floor or the other arm", not contacts, json.dumps(contacts))
    if not args.skip_thermal:
        name = names[0]
        session.post("reset", {"arm": name})
        session.post("sim/temperature", {"arm": name, "joint": 2, "celsius": 80})
        state = wait_until(session, lambda s: s["arms"][name]["temperature_warning"], 3)
        arm = state["arms"][name]
        check(results, "80 C shows a warning and keeps the arm running", bool(arm["temperature_warning"]) and not arm["arm_fault"], f"warning {arm['temperature_warning']!r}, fault {arm['fault']!r}")
        session.post("sim/temperature", {"arm": name, "joint": 2, "celsius": 104})
        state = wait_until(session, lambda s: s["arms"][name]["fault"], 3)
        arm = state["arms"][name]
        check(results, "above 100 C stops motion and holds", "100 C" in (arm["arm_fault"] or "") and arm["torque"], f"fault {arm['fault']!r}, torque {arm['torque']}")
        response = session.post("reset", {"arm": name}, ok=False)
        check(results, "Reset refuses while the joint is still hot", response.status_code == 409 and "still" in response.text, response.text)
        session.post("sim/temperature", {"arm": name, "joint": 2, "celsius": 70})
        time.sleep(1.0)
        response = session.post("reset", {"arm": name}, ok=False)
        session.post("power", {"arm": name, "home": False}, ok=False)
        state = session.state()
        arm = state["arms"][name]
        check(results, "after cooling, Reset and Power on clear the fault", response.status_code == 200 and not arm["arm_fault"] and arm["torque"], f"reset {response.status_code}, fault {arm['arm_fault']!r}")
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    if args.report:
        with open(args.report, "w") as handle:
            json.dump({"results": results, "mounts": {n: state["arms"][n].get("mount") for n in names}}, handle, indent=2)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
