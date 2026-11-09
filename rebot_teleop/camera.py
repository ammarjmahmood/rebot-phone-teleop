import io
import threading
import time


class UsbCamera:
    def __init__(self, device, width=640, height=480, fps=30, quality=75):
        self.device = device
        self.size = (width, height)
        self.fps = fps
        self.quality = quality
        self.jpeg = None
        self.at = 0.0
        self.note = "starting"
        self.wanted = 0.0
        self.running = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.running.set()
            self.thread = threading.Thread(target=self._run, name=f"camera {self.device}", daemon=True)
            self.thread.start()

    def close(self):
        self.running.clear()

    def _open(self, cv2):
        source = int(self.device) if str(self.device).isdigit() else str(self.device)
        capture = cv2.VideoCapture(source, cv2.CAP_V4L2) if hasattr(cv2, "CAP_V4L2") else cv2.VideoCapture(source)
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _run(self):
        try:
            import cv2
        except ImportError:
            self.note = "OpenCV is missing; install it with: uv pip install opencv-python-headless"
            return
        capture = None
        while self.running.is_set():
            if time.monotonic() - self.wanted > 5.0:
                if capture is not None:
                    capture.release()
                    capture = None
                    self.note = "idle"
                time.sleep(0.2)
                continue
            if capture is None:
                capture = self._open(cv2)
                if not capture.isOpened():
                    self.note = f"cannot open {self.device}"
                    capture.release()
                    capture = None
                    time.sleep(2.0)
                    continue
                self.note = "streaming"
            ok, image = capture.read()
            if not ok:
                self.note = f"no frames from {self.device}"
                capture.release()
                capture = None
                time.sleep(1.0)
                continue
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if ok:
                self.jpeg = encoded.tobytes()
                self.at = time.monotonic()
        if capture is not None:
            capture.release()

    def frame(self):
        self.wanted = time.monotonic()
        self.start()
        return self.jpeg if self.jpeg and time.monotonic() - self.at < 2.0 else None

    def status(self):
        return {"kind": "usb", "device": str(self.device), "note": self.note}


class SimCamera:
    def __init__(self, world, channel, quality=80):
        self.world = world
        self.channel = channel
        self.quality = quality
        self.encoded_at = None
        self.jpeg = None
        self.note = "simulated"

    def start(self):
        pass

    def close(self):
        pass

    def frame(self):
        latest = self.world.camera_frame(self.channel)
        if latest is None:
            return None
        at, pixels = latest
        if at != self.encoded_at:
            from PIL import Image
            buffer = io.BytesIO()
            Image.fromarray(pixels).save(buffer, format="JPEG", quality=self.quality)
            self.jpeg = buffer.getvalue()
            self.encoded_at = at
        return self.jpeg

    def status(self):
        return {"kind": "sim", "device": self.channel, "note": self.note}


class SimView:
    def __init__(self, world, quality=80):
        self.world = world
        self.quality = quality
        self.encoded_at = None
        self.jpeg = None

    def frame(self, yaw, pitch, distance, height):
        latest = self.world.overview_frame(yaw, pitch, distance, height)
        if latest is None:
            return None
        at, pixels = latest
        if at != self.encoded_at:
            from PIL import Image
            buffer = io.BytesIO()
            Image.fromarray(pixels).save(buffer, format="JPEG", quality=self.quality)
            self.jpeg = buffer.getvalue()
            self.encoded_at = at
        return self.jpeg
