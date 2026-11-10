import asyncio
import json
import logging
from mcp.server.mcpserver import Image, MCPServer
from rebot_teleop.agent import API, Robot, RobotError, run_code

INSTRUCTIONS = """You drive a real two armed reBot (two Seeed B601 RS arms on the sides of a post, a torso depth camera, a camera on each wrist, a motorised table lift).
Work like Code as Policies: call look_all to see every camera and both arms, write a short Python policy against the robot API and run it with run_policy, then look_all again to check.
Batch several moves into one policy. Moves are smooth and never refused: an out of reach target goes as close as it can and the result says how far off it was.
power_on holds the arm still where it is. The arms work on their own sides (left arm y > 0, right arm y < 0).

""" + API

server = MCPServer("rebot", instructions=INSTRUCTIONS)
logging.getLogger("httpx").setLevel(logging.WARNING)
_robot = None


def robot():
    global _robot
    if _robot is None:
        _robot = Robot()
    return _robot


def report(value):
    return json.dumps(value, indent=1, default=str)


def guarded(function, *args, **kwargs):
    try:
        return report(function(*args, **kwargs))
    except RobotError as error:
        return f"Error: {error}"


def picture(camera):
    return Image(data=robot().jpeg(camera), format="jpeg")


@server.tool(description="The Python robot API that run_policy code can call, with units and the world frame")
def robot_api() -> str:
    return API


@server.tool(description="Joint angles, tip position, gripper width, power and notes for both arms")
def get_state() -> str:
    return guarded(robot().state)


@server.tool(description="Power arms on; they hold still exactly where they are. arm is left, right or empty for both")
def power_on(arm: str = "") -> str:
    return guarded(robot().power_on, arm or None)


@server.tool(description="Turn arm torque off so the arm can be moved by hand; it will sag under gravity, so support it first")
def power_off(arm: str = "") -> str:
    return guarded(robot().power_off, arm or None)


@server.tool(description="Look through one camera: torso (fixed depth camera that sees both arms and the table), left or right (wrist cameras)")
def look(camera: str = "torso") -> Image | str:
    try:
        return picture(camera)
    except RobotError as error:
        return f"No picture: {error}"


@server.tool(description="Every camera picture (torso, left wrist, right wrist) followed by the state of both arms, in one call")
def look_all() -> list:
    parts = []
    for camera in ("torso", "left", "right"):
        parts.append(f"{camera} camera:")
        try:
            parts.append(picture(camera))
        except RobotError as error:
            parts.append(f"no picture: {error}")
    parts.append(guarded(robot().state))
    return parts


@server.tool(description="Move one arm smoothly. Give one of joints_deg (six angles), tip_xyz (world metres) or tip_delta (metres from now). rotation: empty keeps the hand angle, 'down' points the gripper down, or a yaw in degrees as text for top down at that yaw. gripper_m optional (0 closed to 0.05 open)")
def move_arm(arm: str, joints_deg: list[float] | None = None, tip_xyz: list[float] | None = None, tip_delta: list[float] | None = None, rotation: str = "", gripper_m: float | None = None, speed_deg_s: float = 45.0) -> str:
    rot = None
    if rotation:
        try:
            rot = float(rotation)
        except ValueError:
            rot = rotation
    body = {"arm": arm, "joints_deg": joints_deg, "tip_xyz": tip_xyz, "tip_delta": tip_delta, "rotation": rot, "gripper_m": gripper_m, "speed_deg_s": speed_deg_s}
    return guarded(robot()._call, "POST", "/api/move", body)


@server.tool(description="Go to a named pose: home (hanging), ready (over the table) or any pose saved on the dashboard. arm empty means both")
def go_pose(pose: str, arm: str = "") -> str:
    return guarded(robot().go, pose, arm or None)


@server.tool(description="Open or close a gripper; width in metres, 0 closed to 0.05 open")
def set_gripper(arm: str, width_m: float) -> str:
    return guarded(robot().grip, arm, width_m)


@server.tool(description="World xyz for pixels of a camera picture. The torso camera uses its depth; wrist cameras intersect a horizontal plane at plane_z (default the table top)")
def locate(camera: str, pixels: list[list[float]], plane_z: float | None = None) -> str:
    return guarded(robot().locate, camera, pixels, plane_z)


@server.tool(description="Find coloured objects (red, orange, yellow, green, blue) in a camera picture, with pixel, area and world xyz")
def find_color(camera: str, color: str) -> str:
    return guarded(robot().find_color, camera, color)


@server.tool(description="Run a Python policy (Code as Policies). The code gets robot, np, math, time and RobotError. Use print to report. Runs in its own process; the arms hold if it fails or times out")
async def run_policy(code: str, timeout_s: float = 180.0) -> str:
    result = await asyncio.to_thread(run_code, code, timeout_s)
    return ("" if result["ok"] else "[policy failed] ") + result["output"]


@server.tool(description="List the motion sequences saved in the dashboard Sequencer tab")
def list_sequences() -> str:
    return guarded(robot()._call, "GET", "/api/seq")


@server.tool(description="Play a saved sequence and wait until it finishes")
def play_sequence(name: str) -> str:
    return guarded(robot().play, name)


@server.tool(description="Move the table lift up or down for some seconds, then stop")
def table_lift(action: str, seconds: float = 1.0) -> str:
    return guarded(robot().lift, action, seconds)


@server.tool(description="Clear a held fault such as an over temperature hold, so the arms accept moves again")
def clear_faults() -> str:
    return guarded(robot()._call, "POST", "/api/clear")


@server.tool(description="Hold every arm where it is right now and stop any sequence or lift motion")
def stop() -> str:
    return guarded(robot().stop)


def main():
    server.run()


if __name__ == "__main__":
    main()
