import json
import subprocess
import sys
import threading
import time
from pathlib import Path
import numpy as np
import pinocchio as pin


ROOT = Path(__file__).resolve().parents[1]
HEBI_CAMERA_OFFSET = np.array([0.0, -0.02, 0.04])


def rotation_from_quaternion(quaternion_wxyz):
    w, x, y, z = (float(v) for v in quaternion_wxyz)
    norm = (w * w + x * x + y * y + z * z) ** 0.5
    if not norm or not np.isfinite(norm):
        return None
    return pin.Quaternion(w / norm, x / norm, y / norm, z / norm).toRotationMatrix()


class PushSource:
    def __init__(self):
        self.lock = threading.Lock()
        self.sample = None
        self.status = "waiting"

    def start(self, addresses=()):
        self.status = "waiting"

    def stop(self):
        with self.lock:
            self.sample = None
        self.status = "stopped"

    def push(self, position, quaternion_wxyz, inputs):
        rotation = rotation_from_quaternion(quaternion_wxyz)
        position = np.asarray(position, dtype=float)
        if rotation is None or position.shape != (3,) or not np.all(np.isfinite(position)):
            return
        with self.lock:
            self.sample = (time.monotonic(), position, rotation, dict(inputs))
            self.status = "streaming"

    def latest(self):
        with self.lock:
            return self.sample

    def send(self, command):
        pass


class HebiSource(PushSource):
    def __init__(self, command=None):
        super().__init__()
        self.command = command or [str(ROOT / ".venv-hebi/bin/python"), str(ROOT / "scripts/hebi_bridge.py")]
        self.process = None
        self.status = "stopped"

    def start(self, addresses=()):
        if self.process and self.process.poll() is None:
            return
        command = list(self.command)
        for address in addresses:
            command += ["--address", address]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as error:
            self.status = "unavailable"
            raise ValueError("The HEBI bridge could not start; run scripts/setup_hebi.sh first") from error
        self.status = "searching"
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("status") == "pose":
                rotation = rotation_from_quaternion(message["quaternion_wxyz"])
                if rotation is None:
                    continue
                position = np.asarray(message["position"], dtype=float) - rotation @ HEBI_CAMERA_OFFSET
                with self.lock:
                    self.sample = (time.monotonic(), position, rotation, dict(message.get("inputs", {})))
                    self.status = "streaming"
            else:
                self.status = message.get("status", self.status)
        self.status = "stopped"

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        super().stop()

    def send(self, command):
        if self.process and self.process.poll() is None and self.process.stdin:
            try:
                self.process.stdin.write(json.dumps(command) + "\n")
                self.process.stdin.flush()
            except OSError:
                pass
