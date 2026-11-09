import argparse
import json
import math
import sys
import time
from pathlib import Path
import httpx
import numpy as np
import pinocchio as pin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rebot_teleop.kinematics import Kinematics, mount_placement
from rebot_teleop.rs_arm import LIMITS_DEG

URDF = Path(__file__).resolve().parents[1] / "assets" / "rebot_rs.urdf"
STEP_DEG = 20.0
HOME_TOLERANCE_DEG = 8.0


class Session:
    def __init__(self, url, code):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=15)
        response = self.client.post("/api/session", json={"token": code} if code else {})
        if response.status_code >= 400:
            raise SystemExit(f"Pairing failed: {response.text}")

    def post(self, path, body):
        response = self.client.post("/api/" + path, json=body)
        if response.status_code >= 400:
            raise SystemExit(f"{path}: {response.text}")
        return response.json()

    def arm(self, name):
        return self.client.get("/api/state").json()["arms"][name]


def model(arm):
    kin = Kinematics(URDF, mount_placement(**arm["mount"]))
    kin.lower = np.radians([low for low, _ in LIMITS_DEG])
    kin.upper = np.radians([high for _, high in LIMITS_DEG])
    return kin


def describe(kin, q, joint):
    moved = q.copy()
    moved[joint] += math.radians(STEP_DEG)
    before = kin.points(q)
    after = kin.points(moved)
    shift = after[7] - before[7]
    side = 1.0 if kin.mount.translation[1] >= 0 else -1.0
    if np.linalg.norm(shift) > 0.02:
        biggest = np.max(np.abs(shift))
        words = []
        if abs(shift[2]) > 0.35 * biggest:
            words.append("up" if shift[2] > 0 else "down")
        if abs(shift[0]) > 0.35 * biggest:
            words.append("forward, toward the eyes" if shift[0] > 0 else "backward, away from the eyes")
        if abs(shift[1]) > 0.35 * biggest:
            words.append("outward, away from the post" if shift[1] * side > 0 else "inward, toward the post")
        return f"turn joint {joint + 1} so the gripper tip goes " + " and ".join(words)
    _, rotation = kin.pose(q)
    _, turned = kin.pose(moved)
    axis = pin.log3(turned @ rotation.T)
    pointing = before[7] - before[6]
    sense = "clockwise" if axis @ pointing > 0 else "counterclockwise"
    return f"twist joint {joint + 1} {sense} as seen from behind the wrist looking toward the fingertips"


def wait_move(session, name, baseline, threshold, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        q = np.radians(session.arm(name)["joints_deg"])
        if np.max(np.abs(q - baseline)) > math.radians(threshold):
            break
        time.sleep(0.1)
    else:
        return None
    stable = None
    last = q
    while time.monotonic() < end:
        time.sleep(0.15)
        q = np.radians(session.arm(name)["joints_deg"])
        if np.max(np.abs(q - last)) < math.radians(0.7):
            stable = stable or time.monotonic()
            if time.monotonic() - stable > 0.8:
                return q
        else:
            stable = None
        last = q
    return q


def wait_back(session, name, baseline, joint, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        q = np.radians(session.arm(name)["joints_deg"])
        if abs(q[joint] - baseline[joint]) < math.radians(4):
            return True
        time.sleep(0.15)
    return False


def fake_hand(session, name, start, joint, amount_deg, fault):
    target = start.copy()
    index = joint
    sign = 1.0
    if fault == "flip" and joint == 1:
        sign = -1.0
    if fault == "swap" and joint in (3, 4):
        index = 7 - joint
    target[index] += math.radians(sign * amount_deg)
    for u in np.linspace(0, 1, 12)[1:]:
        session.post("sim/hand", {"arm": name, "joints_deg": np.degrees(start + (target - start) * u).tolist()})
        time.sleep(0.06)
    return target


def check(results, name, passed, detail):
    results.append({"check": name, "passed": bool(passed), "detail": detail})
    print(("PASS " if passed else "FAIL ") + name + ": " + detail, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Hand guided check that each real joint matches the URDF joint: order, direction and home angles")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--code", default="")
    parser.add_argument("--arm", action="append", default=[])
    parser.add_argument("--threshold", type=float, default=10.0, help="Degrees a joint must move before it counts")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--simulate", action="store_true", help="Against a --sim server, move the simulated joints instead of waiting for your hands")
    parser.add_argument("--fault", choices=["none", "flip", "swap"], default="none", help="With --simulate, pretend J2 reads backwards or J4 and J5 are swapped, to prove the check catches it")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    session = Session(args.url, args.code)
    state = session.client.get("/api/state").json()
    if args.simulate and not state.get("sim"):
        sys.exit("--simulate only works against a server started with --sim")
    names = args.arm or list(state["arms"])
    results = []
    for name in names:
        arm = state["arms"][name]
        if arm["torque"]:
            sys.exit(f"The {name} arm has its motors on. Support the arm, press Release torque, then run this again")
        session.post("handguide", {"arm": name, "on": True})
    time.sleep(0.5)
    for name in names:
        arm = session.arm(name)
        kin = model(arm)
        q = np.radians(arm["joints_deg"])
        home = np.radians(arm["home_deg"])
        print(f"\n{name} arm, motors off, reading joints by hand guiding")
        print("  joint   reads   home in code   difference")
        worst = 0.0
        for i in range(6):
            difference = math.degrees(q[i] - home[i])
            worst = max(worst, abs(difference))
            print(f"  J{i + 1}   {math.degrees(q[i]):7.1f}   {math.degrees(home[i]):7.1f}   {difference:+7.1f}")
        check(results, f"{name} rest pose matches the home angles in the code", worst < HOME_TOLERANCE_DEG, f"largest difference {worst:.1f} deg; if the arm is resting in its real start pose and this fails, save these readings as home_deg")
        inside = all(kin.lower[i] - 0.05 <= q[i] <= kin.upper[i] + 0.05 for i in range(6))
        check(results, f"{name} readings are inside the URDF joint limits", inside, "readings " + " ".join(f"{v:.1f}" for v in np.degrees(q)))
        for joint in range(6):
            baseline = np.radians(session.arm(name)["joints_deg"])
            instruction = describe(kin, baseline, joint)
            print(f"\n{name} J{joint + 1}: {instruction}, about 20 to 30 degrees, then hold still")
            if args.simulate:
                fake_hand(session, name, baseline, joint, 25.0, args.fault)
            moved = wait_move(session, name, baseline, args.threshold, args.timeout)
            if moved is None:
                check(results, f"{name} J{joint + 1} moved", False, "no joint moved; skipped")
                continue
            delta = np.degrees(moved - baseline)
            biggest = int(np.argmax(np.abs(delta)))
            others = [f"J{i + 1} {delta[i]:+.0f}" for i in range(6) if i not in (joint, biggest) and abs(delta[i]) > 4]
            if biggest != joint:
                check(results, f"{name} J{joint + 1} is the joint that reads the change", False, f"J{biggest + 1} changed by {delta[biggest]:+.1f} deg instead; the motor IDs or the joint order do not match the URDF")
            else:
                check(results, f"{name} J{joint + 1} is the joint that reads the change", True, f"changed {delta[joint]:+.1f} deg" + (f", also {', '.join(others)}" if others else ""))
                check(results, f"{name} J{joint + 1} direction matches the URDF", delta[joint] > 0, "reads positive, same as the model" if delta[joint] > 0 else "reads negative; this motor turns the opposite way to the URDF axis, so IK and gravity compensation would push the wrong way")
            print(f"Now put J{joint + 1} back where it was")
            if args.simulate:
                fake_hand(session, name, moved, joint, 0.0, "none")
                session.post("sim/hand", {"arm": name, "joints_deg": np.degrees(baseline).tolist()})
            wait_back(session, name, baseline, joint if biggest == joint else biggest, args.timeout)
    for name in names:
        session.post("handguide", {"arm": name, "on": False})
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    if args.report:
        Path(args.report).write_text(json.dumps({"results": results}, indent=2))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
