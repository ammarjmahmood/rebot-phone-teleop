import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from mcp.server.mcpserver import Image, MCPServer
from rebot_teleop.agent import GUIDE, Robot, RobotError

GUIDE_FILE = Path(__file__).resolve().parents[1] / "docs" / "ROBOT_GUIDE.md"
INSTRUCTIONS = """\
You control a two armed reBot robot (Seeed B601 RS arms on the sides of a post, phone eyes on top) through a
teleop server, usually the PyBullet simulator. Work like Code as Policies: read the state, look through the
cameras, then write a short Python policy against the robot API and run it with run_policy. Every move is planned
around the post, head and table and checked before anything moves; a refused move explains why.
Call robot_guide first: it explains the frames, what each joint does, where each arm can reach (the arms share
no workspace) and the develop, freeze, evaluate loop for tasks.
""" + GUIDE


server = MCPServer("rebot", instructions=INSTRUCTIONS)
logging.getLogger("httpx").setLevel(logging.WARNING)
robot = None
POLICY_TIMEOUT_S = 180.0


def connect():
    global robot
    if robot is None:
        robot = Robot()
    return robot


def report(value):
    return json.dumps(value, indent=1, default=str)


@server.tool(description="Show the Python robot API that run_policy code can call, with units and the world frame")
def robot_api() -> str:
    return GUIDE


@server.tool(description="The full robot guide: body, world frame, poses, what each joint does, reach limits, cameras, tasks and safety rules. Read it before the first move")
def robot_guide() -> str:
    return GUIDE_FILE.read_text() if GUIDE_FILE.exists() else GUIDE


@server.tool(description="List the simulator benchmark tasks with their instructions")
def list_tasks() -> str:
    try:
        status = connect().task_status()
        return report(status["tasks"])
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="Reset a simulator task to a seeded layout (development seeds 0 to 99). Moves both arms to the ready pose and spawns the table and objects")
def task_reset(name: str, seed: int = 0) -> str:
    try:
        return report(connect().task(name, seed))
    except RobotError as error:
        return f"Refused: {error}"


@server.tool(description="Whether the current simulator task is solved, with a detail line")
def task_status() -> str:
    try:
        status = connect().task_status()
        status.pop("tasks", None)
        return report(status)
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="Simulator only: ground truth positions of the table and objects. Use while developing; evaluations hide it, so final policies must use cameras")
def objects() -> str:
    try:
        return report(connect().objects())
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="World xyz for pixels of the last picture from a camera (call look first). In the simulator it uses depth; on the real robot it intersects with a horizontal plane at plane_z, by default the table top")
def locate(camera: str, pixels: list[list[float]], plane_z: float | None = None) -> str:
    try:
        return report(connect().locate(camera, [tuple(pixel) for pixel in pixels], plane_z))
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="Freeze and evaluate a policy file: runs python -m rebot_teleop.dojo eval on evaluation seeds with ground truth hidden and returns the success rate")
async def evaluate_policy(task: str, policy_path: str, seeds: str = "100-109") -> str:
    current = connect()
    env = os.environ.copy()
    env.pop("REBOT_CODE", None)
    if current.session:
        env["REBOT_SESSION"] = current.session
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "rebot_teleop.dojo", "eval", task, "--policy", policy_path, "--seeds", seeds, "--url", current.url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    output, _ = await process.communicate()
    return output.decode(errors="replace")[-6000:]


@server.tool(description="Joint angles in degrees, gripper tip position in metres, readiness, faults and any simulated contacts for every arm")
def get_state() -> str:
    try:
        return report(connect().state())
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="Move one arm. Give exactly one of joints_deg (six angles), tip_xyz (world metres) or tip_delta (metres relative to now); gripper_m optional (0 closed to 0.05 open). dry_run checks reachability without moving")
def move_arm(arm: str, joints_deg: list[float] | None = None, tip_xyz: list[float] | None = None, tip_delta: list[float] | None = None, gripper_m: float | None = None, dry_run: bool = False) -> str:
    try:
        spec = {"joints_deg": joints_deg, "tip_xyz": tip_xyz, "tip_delta": tip_delta, "gripper_m": gripper_m}
        result = connect().move(dry_run=dry_run, **{arm: spec})
        if not dry_run:
            result["now"] = connect().state()["arms"][arm]
        return report(result)
    except RobotError as error:
        return f"Refused: {error}"


@server.tool(description="Open or close a gripper; width in metres, 0 closed to 0.05 open")
def set_gripper(arm: str, width_m: float) -> str:
    try:
        return report(connect().grip(arm, width_m))
    except RobotError as error:
        return f"Refused: {error}"


@server.tool(description="Run plain words through the command parser, for example 'raise left hand then wave' or 'move hands forward 5 cm'")
def say(text: str) -> str:
    try:
        return report(connect().say(text))
    except RobotError as error:
        return f"Refused: {error}"


@server.tool(description="Send both arms to their hanging home pose")
def home() -> str:
    try:
        return report(connect().home())
    except RobotError as error:
        return f"Refused: {error}"


@server.tool(description="Stop all motion right now")
def stop() -> str:
    try:
        return report(connect().stop())
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="Look through a camera and keep the picture for locate: 'left' or 'right' wrist camera, or 'overview' for a view of the whole simulated robot. yaw and pitch in degrees turn the overview camera around the robot: yaw 90 looks at the face, 180 at the left side, 0 at the right side, 270 at the back; pitch -80 looks from above. Overview pictures are 640x480, wrist pictures 320x240 in the simulator")
def look(camera: str = "overview", yaw: float = 120.0, pitch: float = -15.0, distance: float = 1.9) -> Image | str:
    try:
        current = connect()
        params = {"yaw": yaw, "pitch": pitch, "distance": distance} if camera == "overview" else {}
        response = current.client.get(f"/api/camera/{camera}/capture", params=params)
        if response.status_code >= 400:
            return f"No picture: {response.text}"
        import base64
        return Image(data=base64.b64decode(response.json()["image_jpeg_base64"]), format="jpeg")
    except RobotError as error:
        return f"Error: {error}"


@server.tool(description="Run a Python policy (Code as Policies). The code gets robot, np, math and time. Use print to report. Runs in its own process with a time limit; the arms stop if it fails or times out")
async def run_policy(code: str, timeout_s: float = 120.0) -> str:
    current = connect()
    env = os.environ.copy()
    env.pop("REBOT_CODE", None)
    if current.session:
        env["REBOT_SESSION"] = current.session
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(code)
        path = handle.name
    try:
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "rebot_teleop.agent", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=min(float(timeout_s), POLICY_TIMEOUT_S))
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            robot.stop()
            return f"Stopped: the policy ran longer than {timeout_s:.0f} s and the arms were stopped"
        text = output.decode(errors="replace")
        return text[-12000:] if text else f"[exit {process.returncode}, no output]"
    finally:
        Path(path).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="MCP server that lets Claude Code, Codex, Cursor, Antigravity or any MCP client drive the reBot through the teleop server")
    parser.add_argument("--url", default=None, help="Teleop server, default http://127.0.0.1:8080")
    parser.add_argument("--code", default=None, help="Pairing code when the server is on another computer")
    parser.add_argument("--http", type=int, default=0, metavar="PORT", help="Serve MCP over HTTP at http://127.0.0.1:PORT/mcp instead of stdio, for sandboxed agents such as snap installed Codex")
    args = parser.parse_args()
    if args.url:
        os.environ["REBOT_URL"] = args.url
    if args.code:
        os.environ["REBOT_CODE"] = args.code
    if args.http:
        connect()
        print(f"reBot MCP server at http://127.0.0.1:{args.http}/mcp driving {robot.url}", file=sys.stderr, flush=True)
        server.run("streamable-http", host="127.0.0.1", port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
