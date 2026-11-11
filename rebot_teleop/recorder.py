import json
import threading
import time
from rebot_teleop import config as C

STATE_HZ = 50.0
ARM_FIELDS = ("connected", "powered", "joints_deg", "command_deg", "velocity_deg_s", "effort_nm", "gripper_m")


class Recorder:
    def __init__(self, robot, cameras, teleop, root=None):
        self.robot = robot
        self.cameras = cameras
        self.teleop = teleop
        self.root = root or C.DATA / "recordings"
        self.lock = threading.Lock()
        self.running = threading.Event()
        self.threads = []
        self.path = None
        self.started = 0.0
        self.ended = 0.0
        self.samples = 0
        self.frames = {}
        self.error = None

    def start(self):
        with self.lock:
            if self.running.is_set():
                raise ValueError("Already recording")
            self.path = self.root / time.strftime("%Y%m%d-%H%M%S")
            self.path.mkdir(parents=True, exist_ok=True)
            names = list(self.cameras.cameras)
            meta = {"started": time.time(), "state_hz": STATE_HZ, "arms": list(self.robot.units), "cameras": names, "motors": "joints in degrees, gripper in metres"}
            (self.path / "meta.json").write_text(json.dumps(meta, indent=1))
            self.samples, self.error = 0, None
            self.frames = {name: 0 for name in names}
            self.started, self.ended = time.time(), 0.0
            self.running.set()
            self.threads = [threading.Thread(target=self._guard, args=(job,), name=f"record-{job.__name__}", daemon=True) for job in (self._states, self._images)]
            for thread in self.threads:
                thread.start()
        return self.status()

    def stop(self):
        with self.lock:
            if self.running.is_set():
                self.ended = time.time()
            self.running.clear()
            for thread in self.threads:
                thread.join(timeout=2.0)
            self.threads = []
        return self.status()

    def _guard(self, job):
        try:
            job()
        except Exception as error:
            self.error = str(error)
            self.ended = time.time()
            self.running.clear()

    def _states(self):
        period = 1.0 / STATE_HZ
        next_tick = time.perf_counter()
        with open(self.path / "states.jsonl", "w") as out:
            while self.running.is_set():
                row = {"t": round(time.time(), 4), "teleop": bool(self.teleop.active), "arms": {}}
                for name, unit in self.robot.units.items():
                    status = unit.arm.status()
                    row["arms"][name] = {key: status[key] for key in ARM_FIELDS}
                out.write(json.dumps(row) + "\n")
                self.samples += 1
                next_tick += period
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.perf_counter()

    def _images(self):
        files = {name: (open(self.path / f"{name}.mjpg", "wb"), open(self.path / f"{name}.times.txt", "w")) for name in self.cameras.cameras}
        last = {}
        try:
            while self.running.is_set():
                wrote = False
                for name, camera in self.cameras.cameras.items():
                    jpeg, count = camera.jpeg_bytes()
                    if jpeg and count != last.get(name):
                        last[name] = count
                        video, times = files[name]
                        video.write(jpeg)
                        times.write(f"{time.time():.4f}\n")
                        self.frames[name] += 1
                        wrote = True
                if not wrote:
                    time.sleep(0.005)
        finally:
            for video, times in files.values():
                video.close()
                times.close()

    def status(self):
        recording = self.running.is_set()
        end = time.time() if recording else self.ended
        return {"recording": recording, "name": self.path.name if self.path else None, "path": str(self.path) if self.path else None, "seconds": round(end - self.started, 1) if self.started else 0.0, "samples": self.samples, "frames": dict(self.frames), "error": self.error}
