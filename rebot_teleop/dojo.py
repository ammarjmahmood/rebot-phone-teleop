import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from rebot_teleop.agent import Robot, RobotError

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "policies"
RESULTS = ROOT / "results"


def seeds_from(text):
    seeds = []
    for part in str(text).split(","):
        if "-" in part:
            a, b = part.split("-")
            seeds.extend(range(int(a), int(b) + 1))
        elif part.strip():
            seeds.append(int(part))
    return seeds


def run_episode(robot, task, seed, policy, timeout, camera_only):
    started = time.monotonic()
    try:
        robot.task(task, seed)
    except RobotError as error:
        return {"seed": seed, "success": False, "detail": f"reset failed: {error}", "seconds": 0.0, "output": ""}
    env = os.environ.copy()
    env.pop("REBOT_CODE", None)
    if robot.session:
        env["REBOT_SESSION"] = robot.session
    env["REBOT_URL"] = robot.url
    if camera_only:
        env["REBOT_HIDE_OBJECTS"] = "1"
    try:
        finished = subprocess.run([sys.executable, "-m", "rebot_teleop.agent", str(policy)], capture_output=True, text=True, timeout=timeout, env=env)
        output = finished.stdout + finished.stderr
    except subprocess.TimeoutExpired as expired:
        robot.stop()
        output = (expired.stdout or "") if isinstance(expired.stdout, str) else ""
        output += f"\n[timed out after {timeout:.0f} s]"
    time.sleep(0.5)
    status = robot.task_status()
    return {"seed": seed, "success": bool(status.get("success")), "detail": status.get("detail", ""), "seconds": round(time.monotonic() - started, 1), "output": output[-3000:]}


def evaluate(task, policy, seeds, timeout=180.0, camera_only=True, url=None, code=None, quiet=False):
    robot = Robot(url, code)
    episodes = []
    for seed in seeds:
        episode = run_episode(robot, task, seed, policy, timeout, camera_only)
        episodes.append(episode)
        if not quiet:
            print(f"{task} seed {seed}: {'PASS' if episode['success'] else 'FAIL'} in {episode['seconds']} s, {episode['detail']}", flush=True)
    passed = sum(e["success"] for e in episodes)
    return {"task": task, "policy": str(policy), "camera_only": camera_only, "seeds": seeds, "passed": passed, "total": len(episodes), "success_rate": round(passed / max(1, len(episodes)), 3), "episodes": episodes}


def main():
    parser = argparse.ArgumentParser(description="RoboDojo style develop, freeze and evaluate loop for reBot code as policy in the simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List the tasks")
    reset = sub.add_parser("reset", help="Set up one task layout to work on")
    reset.add_argument("task")
    reset.add_argument("--seed", type=int, default=0)
    run = sub.add_parser("eval", help="Evaluate a frozen policy file over many seeded layouts")
    run.add_argument("task")
    run.add_argument("--policy", default=None, help="Policy file, default policies/<task>.py")
    run.add_argument("--seeds", default="100-119", help="Seeds such as 100-119 or 1,5,9; development seeds are 0 to 99, evaluation seeds start at 100")
    run.add_argument("--timeout", type=float, default=180.0)
    run.add_argument("--allow-ground-truth", action="store_true", help="Let the policy call robot.objects(); results are then not camera only")
    summary = sub.add_parser("summary", help="Table of the latest saved results")
    for each in (reset, run):
        each.add_argument("--url", default=None)
        each.add_argument("--code", default=None)
    args = parser.parse_args()
    if args.command == "list":
        from rebot_teleop.tasks import TASKS
        for name, spec in TASKS.items():
            print(f"{name:14s} {spec['arms']:4s} {spec['text']}")
        return
    if args.command == "reset":
        robot = Robot(args.url, args.code)
        print(json.dumps(robot.task(args.task, args.seed), indent=1))
        return
    if args.command == "summary":
        rows = []
        for path in sorted(RESULTS.glob("*.json")):
            data = json.loads(path.read_text())
            rows.append((data["task"], f"{data['passed']}/{data['total']}", f"{data['success_rate'] * 100:.0f}%", "camera only" if data["camera_only"] else "ground truth", path.name))
        for row in rows:
            print("  ".join(f"{cell:14s}" for cell in row))
        return
    policy = Path(args.policy) if args.policy else POLICIES / f"{args.task}.py"
    if not policy.exists():
        sys.exit(f"No policy at {policy}")
    result = evaluate(args.task, policy, seeds_from(args.seeds), args.timeout, not args.allow_ground_truth, args.url, args.code)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.task}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=1))
    print(f"\n{args.task}: {result['passed']}/{result['total']} passed ({result['success_rate'] * 100:.0f}%), saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
