import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from rebot_teleop.agent import GUIDE, Robot, RobotError, run_policy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "claude-fable-5-2"
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
OUTPUT_LIMIT = 8000
ENV_FILE = Path(os.environ.get("REBOT_TELEOP_DATA", Path.home() / ".rebot-teleop")) / ".env"
USAGE = """examples:
  python -m rebot_teleop.cap "pick up the red block and put it in the bowl"
  python -m rebot_teleop.cap --model claude-opus-5-5 --effort medium "wave with the left arm"

The same loop runs from the dashboard at /cap.
The API key is read from ANTHROPIC_API_KEY or from ANTHROPIC_API_KEY=... in ~/.rebot-teleop/.env.
Like every agent here it only moves the simulator unless the person at the robot sets REBOT_ALLOW_REAL=1."""

SYSTEM = """You control a two arm robot by writing Python. A ready made `robot` object is in scope, along with np, math, time and RobotError.

Use the run_policy tool to run a complete script; it returns what the script printed, or the error. Each run starts a fresh scope, so a script must not rely on variables from an earlier run, but the robot keeps whatever pose and grip the last run left it in. Use the look tool to see through a camera before and after acting. Print the values you need to check, such as positions found by the cameras or the tip after a move.

Work in small steps and check each one. When the task is done, or cannot be done, stop calling tools and reply with a short summary of what happened. Your successful scripts are saved as a policy file, so keep them readable.

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
        "description": "Return a picture from a camera: 'left' or 'right' wrist camera, 'torso' for the fixed camera that sees both arms and the table on the real robot, or 'overview' in the simulator. Also returns the camera intrinsics and pose for use with robot.locate.",
        "input_schema": {
            "type": "object",
            "properties": {"camera": {"type": "string", "enum": ["left", "right", "torso", "overview"]}},
            "required": ["camera"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
]


class Stopped(RobotError):
    pass


def load_key(env_file=ENV_FILE, values=None):
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    found = (values or {}).get("ANTHROPIC_API_KEY", "")
    if not found and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "ANTHROPIC_API_KEY":
                found = value.strip().strip("\"'")
    if found:
        os.environ["ANTHROPIC_API_KEY"] = found
    return bool(found)


def system_prompt():
    guide_path = ROOT / "docs" / "ROBOT_GUIDE.md"
    robot_guide = guide_path.read_text() if guide_path.exists() else ""
    return SYSTEM.format(guide=GUIDE, robot_guide=robot_guide)


def tail(text, limit=OUTPUT_LIMIT):
    return text if len(text) <= limit else "[earlier output cut]\n" + text[-limit:]


def request(client, model, effort, messages, system):
    import anthropic
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


class Run:
    def __init__(self, robot, instruction, model=DEFAULT_MODEL, effort="high", max_turns=20, emit=None, client=None):
        self.robot = robot
        self.instruction = instruction
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.emit = emit or (lambda kind, text, **extra: None)
        self.client = client
        self.cancel = threading.Event()
        self.scripts = []
        self.summary = ""

    def check(self):
        if self.cancel.is_set():
            raise Stopped("Stopped by the operator")

    def tool(self, name, args):
        if name == "run_policy":
            code = args.get("code") if isinstance(args, dict) else None
            if not isinstance(code, str) or not code.strip():
                return [{"type": "text", "text": "run_policy needs a non empty `code` string"}], True
            self.emit("code", code)
            ok, output, took = run_policy(code, self.robot)
            self.check()
            self.emit("output", output.rstrip() or "(no output)", ok=ok, seconds=round(took, 1))
            if ok:
                self.scripts.append(code)
            return [{"type": "text", "text": f"{'finished' if ok else 'FAILED'} in {took:.1f} s\n{tail(output) or '(no output)'}"}], not ok
        if name == "look":
            camera = args.get("camera") if isinstance(args, dict) else None
            if camera not in {"left", "right", "torso", "overview"}:
                return [{"type": "text", "text": "look needs camera 'left', 'right', 'torso' or 'overview'"}], True
            try:
                data = self.robot._get(f"/api/camera/{camera}/capture")
            except RobotError as error:
                self.emit("output", f"Could not look through {camera}: {error}", ok=False)
                return [{"type": "text", "text": f"RobotError: {error}"}], True
            image = data.pop("image_jpeg_base64")
            self.emit("image", f"Looked through the {camera} camera", image=image)
            return [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
                {"type": "text", "text": json.dumps(data)},
            ], False
        return [{"type": "text", "text": f"Unknown tool {name}"}], True

    def run(self):
        import anthropic
        client = self.client or anthropic.Anthropic()
        system = system_prompt()
        messages = [{"role": "user", "content": self.instruction}]
        self.emit("info", f"{self.model}, effort {self.effort}, {'simulator' if self.robot.sim else 'REAL ARMS'}")
        try:
            for _ in range(self.max_turns):
                self.check()
                self.emit("thinking", f"{self.model} is thinking")
                try:
                    response = request(client, self.model, self.effort, messages, system)
                except anthropic.RateLimitError:
                    self.emit("info", "Rate limited; waiting 30 s")
                    self.cancel.wait(30)
                    continue
                self.check()
                messages.append({"role": "assistant", "content": response.content})
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        self.summary = block.text.strip()
                        self.emit("text", self.summary, model=response.model)
                if response.stop_reason == "refusal":
                    detail = response.stop_details.category if response.stop_details else None
                    self.emit("error", f"The model declined ({detail}).")
                    return False
                if response.stop_reason == "max_tokens":
                    messages.append({"role": "user", "content": "Your last reply was cut off. Continue, with shorter scripts."})
                    continue
                calls = [block for block in response.content if block.type == "tool_use"]
                if response.stop_reason != "tool_use" or not calls:
                    return True
                results = []
                for call in calls:
                    self.check()
                    content, failed = self.tool(call.name, call.input)
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": failed})
                messages.append({"role": "user", "content": results})
            self.emit("error", f"Stopped after {self.max_turns} model calls.")
            return False
        except Stopped as error:
            self.emit("error", str(error))
            return False
        except anthropic.APIStatusError as error:
            self.emit("error", f"API error {error.status_code}: {error.message}")
            return False
        except anthropic.APIConnectionError as error:
            self.emit("error", f"Could not reach the API: {error}")
            return False

    def save(self, path=None):
        if not self.scripts:
            return None
        path = Path(path) if path else ROOT / "policies" / "cap" / f"{time.strftime('%Y%m%d_%H%M%S')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"# Code as policy by {getattr(self, 'label', self.model)}: {self.instruction}\n# Run again with: python -m rebot_teleop.agent {path}\n"
        path.write_text(header + "\n\n# next step\n".join(self.scripts) + "\n")
        return path


CLI_PROMPT = """You drive a two arm robot through the rebot tools, Code as Policies style. The arms start in the pose the operator left them in: plan every move from that pose, and never send them home, to ready or to any preset pose unless the task asks for it. Before moving, call robot_guide if you have not, read the state and look through the cameras: the torso camera, when there is one, sees both arms and the table, and the wrist cameras see close up; navigate by what the cameras show, looking again after each step. Write short Python policies against the robot API, run them with run_policy, and keep moves small and slow. When the task is done, or cannot be done, stop and reply with a short summary of what happened."""


def clean_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE", "ANTHROPIC_"))}
    env.pop("REBOT_CODE", None)
    return env


class CliRun:
    def __init__(self, url, instruction, model=DEFAULT_MODEL, effort="high", max_turns=40, emit=None):
        self.url = url
        self.instruction = instruction
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.emit = emit or (lambda kind, text, **extra: None)
        self.cancel = threading.Event()
        self.process = None
        self.scripts = []
        self.pending = {}
        self.summary = ""
        self.sim = True

    def command(self):
        claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        config = {"mcpServers": {"rebot": {"command": "bash", "args": [str(ROOT / "scripts" / "rebot_mcp.sh")], "env": {"REBOT_URL": self.url}}}}
        return [
            claude, "-p", self.instruction, *(["--model", self.model] if self.model else []), "--effort", self.effort,
            "--output-format", "stream-json", "--verbose", "--max-turns", str(self.max_turns),
            "--strict-mcp-config", "--mcp-config", json.dumps(config), "--tools", "",
            "--allowedTools", "mcp__rebot__*", "--permission-mode", "dontAsk",
            "--append-system-prompt", CLI_PROMPT,
        ]

    def stop(self):
        self.cancel.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def tool_use(self, block):
        name = block.get("name", "").removeprefix("mcp__rebot__")
        args = block.get("input") or {}
        self.pending[block.get("id")] = (name, args)
        if name == "run_policy":
            self.emit("code", args.get("code", ""))
        else:
            shown = json.dumps(args) if args else ""
            self.emit("info", f"{name} {shown}".strip())

    def tool_result(self, block):
        name, args = self.pending.pop(block.get("tool_use_id"), ("", {}))
        content = block.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
        text = "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()
        try:
            wrapped = json.loads(text)
            if isinstance(wrapped, dict) and isinstance(wrapped.get("result"), str) and len(wrapped) == 1:
                text = wrapped["result"].strip()
        except ValueError:
            pass
        for part in parts:
            if part.get("type") == "image":
                self.emit("image", f"Looked through the {args.get('camera', 'overview')} camera", image=part.get("source", {}).get("data", ""))
        failed = bool(block.get("is_error")) or text.startswith(("Error", "RobotError", "Stopped", "Traceback")) or "[policy failed" in text
        if name == "run_policy" and not failed:
            self.scripts.append(args.get("code", ""))
        if text and name != "look":
            self.emit("output", text[-6000:], ok=not failed)

    def run(self):
        self.label = self.model or "the CLI default model"
        self.emit("info", f"{self.label} through the Claude CLI, effort {self.effort}")
        self.emit("thinking", "Starting")
        try:
            self.process = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=clean_env(), cwd=ROOT, bufsize=1)
        except OSError as error:
            self.emit("error", f"Could not start the claude CLI: {error}")
            return False
        result = None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                self.emit("info", line[:500])
                continue
            kind = event.get("type")
            if kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        self.summary = block["text"].strip()
                        self.emit("text", self.summary, model=event.get("message", {}).get("model", self.label))
                    elif block.get("type") == "tool_use":
                        self.tool_use(block)
                self.emit("thinking", "Working")
            elif kind == "user":
                content = event.get("message", {}).get("content", [])
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_result":
                        self.tool_result(block)
                self.emit("thinking", "Thinking")
            elif kind == "result":
                result = event
            elif kind == "system" and event.get("subtype") == "init":
                if event.get("model"):
                    self.label = event["model"]
                    self.emit("info", f"Model: {self.label}")
                servers = {server.get("name"): server.get("status") for server in event.get("mcp_servers", [])}
                if servers.get("rebot") != "connected":
                    self.emit("error", f"The robot tools did not connect: {servers.get('rebot')}")
        self.process.wait()
        if self.cancel.is_set():
            self.emit("error", "Stopped by the operator")
            return False
        if result is None:
            self.emit("error", f"The claude CLI exited with code {self.process.returncode} and no result")
            return False
        if result.get("is_error"):
            self.emit("error", str(result.get("result") or result.get("subtype")))
            return False
        cost = result.get("total_cost_usd")
        self.emit("info", f"Finished in {result.get('num_turns', '?')} turns, {round(result.get('duration_ms', 0) / 1000)} s" + (f", about ${cost:.2f} of usage" if cost else ""))
        return True

    save = Run.save


def print_event(kind, text, **extra):
    if kind == "code":
        print(f"\n[running policy]\n{text}", flush=True)
    elif kind == "output":
        print(f"{text}\n[{'finished' if extra.get('ok', True) else 'failed'}{' in ' + str(extra['seconds']) + ' s' if 'seconds' in extra else ''}]", flush=True)
    elif kind == "text":
        print(f"\n{extra.get('model', 'model')}: {text}", flush=True)
    elif kind != "thinking":
        print(f"\n[{text}]", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Code as policy: Claude writes and runs robot scripts until the instruction is done", epilog=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instruction", nargs="+", help="What the robot should do")
    parser.add_argument("--model", default=os.environ.get("REBOT_MODEL", DEFAULT_MODEL), help=f"Claude model, default $REBOT_MODEL or {DEFAULT_MODEL}")
    parser.add_argument("--effort", default=os.environ.get("REBOT_EFFORT", "high"), choices=EFFORTS)
    parser.add_argument("--max-turns", type=int, default=20, help="Most model calls before giving up")
    parser.add_argument("--url", default=None, help="Teleop server, default $REBOT_URL or http://127.0.0.1:8080")
    parser.add_argument("--code", default=None, help="Pairing code when the server is on another computer")
    parser.add_argument("--speed", type=float, default=None, help="Joint speed in degrees per second")
    parser.add_argument("--save", default=None, help="Where to save the successful scripts, default policies/cap/<time>.py")
    args = parser.parse_args()
    try:
        robot = Robot(args.url, args.code, speed_deg_s=args.speed)
        robot.ensure_ready()
    except RobotError as error:
        sys.exit(str(error))
    load_key()
    run = Run(robot, " ".join(args.instruction), args.model, args.effort, args.max_turns, print_event)
    try:
        ok = run.run()
    except KeyboardInterrupt:
        print("\nInterrupted; stopping the arms.")
        robot.stop()
        ok = False
    saved = run.save(args.save)
    if saved:
        print(f"\nSaved the {len(run.scripts)} successful scripts to {saved}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
