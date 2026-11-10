import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from rebot_teleop import config as C

PROMPT = """You are driving the real reBot through the rebot MCP tools. Start with look_all. Write complete Python policies with run_policy, look_all again after each one, and keep going until the instruction is done.
Prefer few, larger policies over many tiny steps. When finished, reply with one short paragraph saying what happened."""


def claude_binary():
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def mcp_config(url):
    return {"mcpServers": {"rebot": {"command": str(C.ROOT / ".venv" / "bin" / "python"), "args": ["-m", "rebot_teleop.mcp_server"], "cwd": str(C.ROOT), "env": {"REBOT_URL": url}}}}


class CapRun:
    def __init__(self, url, instruction, model=None, effort="medium", emit=None, max_turns=60):
        self.url = url
        self.instruction = instruction
        self.model = model
        self.effort = effort
        self.emit = emit or (lambda kind, text, **extra: None)
        self.max_turns = max_turns
        self.process = None
        self.thread = None
        self.cancelled = False
        self.pending = {}

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def command(self):
        return [
            claude_binary(), "-p", self.instruction, *(["--model", self.model] if self.model else []), "--effort", self.effort,
            "--output-format", "stream-json", "--verbose", "--max-turns", str(self.max_turns),
            "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config(self.url)), "--tools", "",
            "--allowedTools", "mcp__rebot__*", "--permission-mode", "dontAsk", "--append-system-prompt", PROMPT,
        ]

    def start(self):
        self.thread = threading.Thread(target=self.run, name="cap", daemon=True)
        self.thread.start()

    def stop(self):
        self.cancelled = True
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"))}
        self.emit("info", f"Claude CLI, model {self.model or 'default'}, effort {self.effort}")
        try:
            self.process = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=str(C.ROOT), bufsize=1)
        except OSError as error:
            self.emit("error", f"Could not start the claude CLI: {error}. Install it and run claude login once on the Jetson")
            return
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                self.emit("info", line[:400])
                continue
            kind = event.get("type")
            if kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        self.emit("text", block["text"].strip())
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "").removeprefix("mcp__rebot__")
                        args = block.get("input") or {}
                        self.pending[block.get("id")] = name
                        if name == "run_policy":
                            self.emit("code", args.get("code", ""))
                        else:
                            self.emit("tool", f"{name} {json.dumps(args) if args else ''}".strip())
            elif kind == "user":
                content = event.get("message", {}).get("content", [])
                for block in content if isinstance(content, list) else []:
                    if block.get("type") != "tool_result":
                        continue
                    name = self.pending.pop(block.get("tool_use_id"), "")
                    parts = block.get("content")
                    parts = parts if isinstance(parts, list) else [{"type": "text", "text": str(parts or "")}]
                    for part in parts:
                        if part.get("type") == "image":
                            self.emit("image", name, image=part.get("source", {}).get("data", ""))
                    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
                    if text:
                        self.emit("output", text[-5000:], ok=not (block.get("is_error") or text.startswith(("Error", "[policy failed"))))
            elif kind == "system" and event.get("subtype") == "init":
                servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers", [])}
                if servers.get("rebot") != "connected":
                    self.emit("error", f"The robot tools did not connect: {servers.get('rebot')}")
                if event.get("model"):
                    self.emit("info", f"Model {event['model']}")
            elif kind == "result":
                self.emit("done", event.get("result") or event.get("subtype", "finished"), cost=event.get("total_cost_usd"))
        self.process.wait()
        if self.cancelled:
            self.emit("error", "Stopped")
