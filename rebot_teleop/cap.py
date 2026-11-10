import argparse
import json
import os
import sys
import time
from pathlib import Path

import anthropic

from rebot_teleop.agent import GUIDE, Robot, RobotError, run_policy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "claude-fable-5-2"
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
OUTPUT_LIMIT = 8000
ENV_FILE = Path(os.environ.get("REBOT_TELEOP_DATA", Path.home() / ".rebot-teleop")) / ".env"
USAGE = """examples:
  python -m rebot_teleop.cap "pick up the red block and put it in the bowl"
  python -m rebot_teleop.cap --model claude-opus-5-5 --effort medium "wave with the left arm"

The API key is read from ANTHROPIC_API_KEY or from ANTHROPIC_API_KEY=... in ~/.rebot-teleop/.env.
Like every agent here it only moves the simulator unless the person at the robot sets REBOT_ALLOW_REAL=1."""

SYSTEM = """You control a two arm robot by writing Python. A ready made `robot` object is in scope, along with np, math, time and RobotError.

Use the run_policy tool to run a complete script; it returns what the script printed, or the error. Each run starts a fresh scope, so a script must not rely on variables from an earlier run, but the robot keeps whatever pose and grip the last run left it in. Use the look tool to see through a camera before and after acting. Print the values you need to check, such as positions found by the cameras or the tip after a move.

Work in small steps and check each one. When the task is done, or cannot be done, stop calling tools and reply with a short summary of what happened. Your last successful scripts are saved as a policy file, so keep them readable.

{guide}

The full robot guide:

{robot_guide}"""

TOOLS = [
    {
        "name": "run_policy",
        "description": "Run a Python script against the robot and return its printed output, or the error and traceback. A refused or failed move stops the script and stops the arms.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Complete Python script using the `robot` object"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
    {
        "name": "look",
        "description": "Return a picture from a camera: 'left' or 'right' wrist camera, or 'overview' in the simulator. Also returns the camera intrinsics and pose for use with robot.locate.",
        "input_schema": {
            "type": "object",
            "properties": {"camera": {"type": "string", "enum": ["left", "right", "overview"]}},
            "required": ["camera"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
]


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ANTHROPIC_API_KEY" and value.strip():
            os.environ["ANTHROPIC_API_KEY"] = value.strip().strip("\"'")


def system_prompt():
    guide_path = ROOT / "docs" / "ROBOT_GUIDE.md"
    robot_guide = guide_path.read_text() if guide_path.exists() else ""
    return SYSTEM.format(guide=GUIDE, robot_guide=robot_guide)


def tail(text, limit=OUTPUT_LIMIT):
    return text if len(text) <= limit else "[earlier output cut]\n" + text[-limit:]


def run_tool(robot, name, args, log):
    if name == "run_policy":
        code = args.get("code") if isinstance(args, dict) else None
        if not isinstance(code, str) or not code.strip():
            return [{"type": "text", "text": "run_policy needs a non empty `code` string"}], True, None
        log(f"\n[running policy]\n{code}\n")
        ok, output, took = run_policy(code, robot)
        log(output + f"[{'finished' if ok else 'failed'} in {took:.1f} s]\n")
        text = tail(output) or "(no output)"
        return [{"type": "text", "text": f"{'finished' if ok else 'FAILED'} in {took:.1f} s\n{text}"}], not ok, code if ok else None
    if name == "look":
        camera = args.get("camera") if isinstance(args, dict) else None
        if camera not in {"left", "right", "overview"}:
            return [{"type": "text", "text": "look needs camera 'left', 'right' or 'overview'"}], True, None
        log(f"\n[looking through {camera}]\n")
        try:
            data = robot._get(f"/api/camera/{camera}/capture")
        except RobotError as error:
            return [{"type": "text", "text": f"RobotError: {error}"}], True, None
        image = data.pop("image_jpeg_base64")
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
            {"type": "text", "text": json.dumps(data)},
        ], False, None
    return [{"type": "text", "text": f"Unknown tool {name}"}], True, None


def request(client, model, effort, messages, system):
    options = dict(
        model=model,
        max_tokens=64000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=TOOLS,
        messages=messages,
        output_config={"effort": effort},
    )
    if model in FALLBACK_MODELS:
        options.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    with client.beta.messages.stream(**options) as stream:
        return stream.get_final_message()


def main():
    parser = argparse.ArgumentParser(description="Code as policy: Claude writes and runs robot scripts until the instruction is done", epilog=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instruction", nargs="+", help="What the robot should do")
    parser.add_argument("--model", default=os.environ.get("REBOT_MODEL", DEFAULT_MODEL), help=f"Claude model, default $REBOT_MODEL or {DEFAULT_MODEL}")
    parser.add_argument("--effort", default=os.environ.get("REBOT_EFFORT", "high"), choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-turns", type=int, default=20, help="Most model calls before giving up")
    parser.add_argument("--url", default=None, help="Teleop server, default $REBOT_URL or http://127.0.0.1:8080")
    parser.add_argument("--code", default=None, help="Pairing code when the server is on another computer")
    parser.add_argument("--speed", type=float, default=None, help="Joint speed in degrees per second")
    parser.add_argument("--save", default=None, help="Where to save the successful scripts, default policies/cap/<time>.py")
    args = parser.parse_args()
    instruction = " ".join(args.instruction)

    try:
        robot = Robot(args.url, args.code, speed_deg_s=args.speed)
        robot.ensure_ready()
    except RobotError as error:
        sys.exit(str(error))
    load_key()
    client = anthropic.Anthropic()
    system = system_prompt()
    messages = [{"role": "user", "content": instruction}]
    log = lambda text: print(text, end="", flush=True)
    good_scripts = []
    log(f"[{args.model}, effort {args.effort}, {'simulator' if robot.sim else 'REAL ARMS'}] {instruction}\n")

    try:
        for turn in range(args.max_turns):
            try:
                response = request(client, args.model, args.effort, messages, system)
            except anthropic.RateLimitError:
                log("\nRate limited; waiting 30 s\n")
                time.sleep(30)
                continue
            except anthropic.APIStatusError as error:
                sys.exit(f"API error {error.status_code}: {error.message}")
            except anthropic.APIConnectionError as error:
                sys.exit(f"Could not reach the API: {error}")
            messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    log(f"\n{response.model}: {block.text}\n")
            if response.stop_reason == "refusal":
                detail = response.stop_details.category if response.stop_details else None
                log(f"\nThe model declined ({detail}). Stopping.\n")
                break
            if response.stop_reason == "max_tokens":
                messages.append({"role": "user", "content": "Your last reply was cut off. Continue, with shorter scripts."})
                continue
            calls = [block for block in response.content if block.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                break
            results = []
            for call in calls:
                content, failed, script = run_tool(robot, call.name, call.input, log)
                if script:
                    good_scripts.append(script)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": failed})
            messages.append({"role": "user", "content": results})
        else:
            log(f"\nStopped after {args.max_turns} model calls.\n")
    except KeyboardInterrupt:
        log("\nInterrupted; stopping the arms.\n")
        robot.stop()

    if good_scripts:
        path = Path(args.save) if args.save else ROOT / "policies" / "cap" / f"{time.strftime('%Y%m%d_%H%M%S')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"# Code as policy by {args.model}: {instruction}\n# Run again with: python -m rebot_teleop.agent {path}\n"
        path.write_text(header + "\n\n# next step\n".join(good_scripts) + "\n")
        log(f"\nSaved the {len(good_scripts)} successful scripts to {path}\n")


if __name__ == "__main__":
    main()
