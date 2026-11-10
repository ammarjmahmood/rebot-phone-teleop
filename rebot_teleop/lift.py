import os
import select
import termios
import threading
import time
from rebot_teleop import config as C

DEADMAN_S = 0.45


class Lift:
    def __init__(self, port=None):
        self.port = port or C.load("lift.json", {}).get("port", C.LIFT_PORT)
        self.fd = None
        self.lock = threading.RLock()
        self.state = "stop"
        self.ticks = None
        self.note = "not connected"
        self.held_until = 0.0
        self.speed = C.load("lift.json", {}).get("speed", 40)
        self.thread = threading.Thread(target=self._watch, name="lift", daemon=True)

    def start(self):
        self.thread.start()

    def _connect(self):
        if not os.path.exists(self.port):
            self.note = "Pico not plugged in"
            return False
        try:
            fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
            self.fd = fd
            self.note = None
            self._send("STOP")
            self._send(f"SPEED:{int(self.speed)}")
            return True
        except OSError as error:
            self.note = f"Lift serial error: {error}"
            self.fd = None
            return False

    def _send(self, text, wait=0.3):
        with self.lock:
            if self.fd is None and not self._connect():
                raise ValueError(self.note or "Lift not connected")
            try:
                while select.select([self.fd], [], [], 0)[0]:
                    if not os.read(self.fd, 4096):
                        break
                os.write(self.fd, (text + "\n").encode())
                end = time.monotonic() + wait
                buffer = b""
                while time.monotonic() < end:
                    ready = select.select([self.fd], [], [], max(0.0, end - time.monotonic()))[0]
                    if not ready:
                        break
                    chunk = os.read(self.fd, 4096)
                    if not chunk:
                        break
                    buffer += chunk
                    if b"\n" in buffer:
                        break
                return buffer.decode(errors="ignore").strip().splitlines()[-1] if buffer.strip() else ""
            except OSError as error:
                self.note = f"Lift lost: {error}"
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
                raise ValueError(self.note)

    def command(self, action):
        action = (action or "stop").lower()
        if action not in ("up", "down", "stop"):
            raise ValueError("Lift action is up, down or stop")
        self._send(action.upper())
        self.state = action
        return self.status()

    def hold(self, direction):
        self.held_until = time.monotonic() + DEADMAN_S
        if self.state != direction:
            self.command(direction)

    def release(self):
        if self.state != "stop" and self.held_until:
            self.held_until = 0.0
            try:
                self.command("stop")
            except ValueError:
                pass

    def stop(self):
        self.held_until = 0.0
        try:
            return self.command("stop")
        except ValueError:
            self.state = "stop"
            return self.status()

    def set_speed(self, percent):
        self.speed = max(10, min(100, int(percent)))
        self._send(f"SPEED:{self.speed}")
        settings = C.load("lift.json", {})
        settings["speed"] = self.speed
        C.save("lift.json", settings)
        return self.status()

    def _watch(self):
        polled = 0.0
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            if self.held_until and now > self.held_until and self.state != "stop":
                self.held_until = 0.0
                try:
                    self.command("stop")
                except ValueError:
                    pass
            if now - polled > 0.5:
                polled = now
                try:
                    reply = self._send("TICKS", wait=0.2)
                    if reply.startswith("TICKS:"):
                        self.ticks = int(reply.split(":")[1])
                except (ValueError, IndexError):
                    time.sleep(1.0)

    def status(self):
        return {"connected": self.fd is not None, "state": self.state, "ticks": self.ticks, "note": self.note, "speed": self.speed}
