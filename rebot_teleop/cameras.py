import os
import threading
import time
import numpy as np
from rebot_teleop import config as C

IDLE_S = 8.0


class UsbCamera:
    def __init__(self, device, width=640, height=480, fps=30, rotate=0):
        self.device = device
        self.size = (width, height, fps)
        self.rotate = rotate
        self.lock = threading.Lock()
        self.jpeg = None
        self.frames = 0
        self.at = 0.0
        self.wanted = 0.0
        self.note = "starting"
        self.running = threading.Event()
        self.thread = None
        self.decoded = (None, -1)

    def start(self):
        if self.thread is None:
            self.running.set()
            self.thread = threading.Thread(target=self._run, name=f"camera {os.path.basename(str(self.device))}", daemon=True)
            self.thread.start()

    def close(self):
        self.running.clear()

    def _open(self, cv2):
        capture = cv2.VideoCapture(str(self.device), cv2.CAP_V4L2)
        width, height, fps = self.size
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        capture.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        return capture

    def _run(self):
        try:
            import cv2
        except ImportError:
            self.note = "OpenCV is missing"
            return
        capture = None
        while self.running.is_set():
            if time.monotonic() - self.wanted > IDLE_S:
                if capture is not None:
                    capture.release()
                    capture = None
                self.note = "idle"
                time.sleep(0.1)
                continue
            if capture is None:
                if not os.path.exists(str(self.device)):
                    self.note = "unplugged"
                    time.sleep(1.0)
                    continue
                capture = self._open(cv2)
                if not capture.isOpened():
                    self.note = f"cannot open {self.device}"
                    capture.release()
                    capture = None
                    time.sleep(1.0)
                    continue
                self.note = "streaming"
            ok, frame = capture.read()
            if not ok or frame is None:
                self.note = "no frames; reopening"
                capture.release()
                capture = None
                time.sleep(0.3)
                continue
            data = frame.tobytes() if frame.ndim == 2 or frame.shape[0] == 1 else None
            if data is None or data[:2] != b"\xff\xd8" or self.rotate:
                image = frame if data is None or data[:2] != b"\xff\xd8" else cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if self.rotate == 180:
                    image = cv2.rotate(image, cv2.ROTATE_180)
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
                data = encoded.tobytes() if ok else None
            if data:
                with self.lock:
                    self.jpeg = data
                    self.frames += 1
                    self.at = time.monotonic()

    def jpeg_bytes(self, quality=75):
        self.wanted = time.monotonic()
        with self.lock:
            return self.jpeg, self.frames

    def frame(self):
        import cv2
        self.wanted = time.monotonic()
        jpeg, count = self.jpeg_bytes()
        if jpeg is None:
            return None, None, 0
        if self.decoded[1] != count:
            self.decoded = (cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR), count)
        return self.decoded[0], None, count

    def status(self):
        return {"kind": "usb", "device": str(self.device), "note": self.note, "frames": self.frames, "age_ms": round((time.monotonic() - self.at) * 1000) if self.at else None}


class Cameras:
    def __init__(self, sim=False):
        settings = C.load("cameras.json", {})
        devices = dict(C.DEFAULT_CAMERAS)
        devices.update(settings.get("devices", {}))
        self.rotate = settings.get("rotate", {})
        self.fov = settings.get("fov_deg", {"left": 70.0, "right": 70.0})
        self.head_pose = settings.get("head_pose", C.HEAD_POSE)
        self.cameras = {}
        if sim:
            return
        for name, device in devices.items():
            if not device:
                continue
            if device == "hp60c":
                from rebot_teleop.hp60c import HP60C
                self.cameras[name] = HP60C(rotate=int(self.rotate.get(name, 0)))
            else:
                self.cameras[name] = UsbCamera(device, rotate=int(self.rotate.get(name, 0)))

    def start(self):
        for camera in self.cameras.values():
            camera.start()

    def close(self):
        for camera in self.cameras.values():
            camera.close()

    def get(self, name):
        if name not in self.cameras:
            raise ValueError(f"No camera named {name}; cameras are {', '.join(self.cameras) or 'none'}")
        return self.cameras[name]

    def status(self):
        return {name: camera.status() for name, camera in self.cameras.items()}


def look_at(eye, target):
    eye, target = np.asarray(eye, dtype=float), np.asarray(target, dtype=float)
    forward = target - eye
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    if np.linalg.norm(right) < 1e-6:
        right = np.array([0.0, -1.0, 0.0])
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    return np.column_stack([right, down, forward])


def pixel_rays(pixels, width, height, intrinsics=None, fov_deg=70.0):
    if intrinsics:
        fx, fy, cx, cy = intrinsics["fx"], intrinsics["fy"], intrinsics["cx"], intrinsics["cy"]
    else:
        fx = fy = (width / 2) / np.tan(np.radians(fov_deg) / 2)
        cx, cy = width / 2, height / 2
    rays = []
    for u, v in pixels:
        rays.append(np.array([(u - cx) / fx, (v - cy) / fy, 1.0]))
    return rays, (fx, fy, cx, cy)
