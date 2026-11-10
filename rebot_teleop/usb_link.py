import shutil
import subprocess
import threading
import time
from rebot_teleop import config as C

PORTS = (C.HTTP_PORT, C.HTTPS_PORT)


class QuestUsb:
    def __init__(self):
        self.adb = shutil.which("adb")
        self.devices = []
        self.state = "adb is not installed; run scripts/install_jetson.sh" if not self.adb else "no Quest on USB"
        self.linked = set()
        self.thread = threading.Thread(target=self._loop, name="quest-usb", daemon=True)

    def start(self):
        if self.adb:
            self.thread.start()

    def _run(self, *args):
        return subprocess.run([self.adb, *args], capture_output=True, text=True, timeout=5)

    def _loop(self):
        while True:
            try:
                self.poll()
            except Exception as error:
                self.state = f"adb error: {error}"
            time.sleep(2.0)

    def poll(self):
        lines = self._run("devices").stdout.splitlines()[1:]
        found = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                found[parts[0]] = parts[1]
        self.devices = [{"serial": s, "state": st} for s, st in found.items()]
        ready = [s for s, st in found.items() if st == "device"]
        self.linked &= set(ready)
        for serial in ready:
            if serial in self.linked:
                continue
            for port in PORTS:
                self._run("-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}")
            self.linked.add(serial)
        if ready:
            self.state = f"Quest on USB: open http://localhost:{C.HTTP_PORT}/quest in the Quest browser"
        elif any(st == "unauthorized" for st in found.values()):
            self.state = "Quest on USB but not allowed yet: put the headset on and accept Allow USB debugging"
        else:
            self.state = "no Quest on USB"

    def status(self):
        return {"adb": bool(self.adb), "state": self.state, "devices": self.devices, "url": f"http://localhost:{C.HTTP_PORT}/quest"}
