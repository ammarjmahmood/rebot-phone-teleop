import io
import math
import threading
import time
import numpy as np


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


def encode(pixels, quality):
    from PIL import Image
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def lens(width, height, fov_horizontal_deg):
    focal = (width / 2) / math.tan(math.radians(fov_horizontal_deg) / 2)
    return {"width": width, "height": height, "fx": focal, "fy": focal, "cx": width / 2, "cy": height / 2}


def optical_pose(position, rotation, offset=0.021):
    forward = rotation[:, 2]
    down = rotation[:, 0]
    right = np.cross(down, forward)
    pose = np.eye(4)
    pose[:3, :3] = np.column_stack([right, down, forward])
    pose[:3, 3] = position + forward * offset
    return pose


def look_at_pose(xyz, target):
    position = np.asarray(xyz, dtype=float)
    forward = np.asarray(target, dtype=float) - position
    if np.linalg.norm(forward) < 1e-6:
        raise ValueError("A fixed camera needs a look_at point away from its position")
    forward = forward / np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    if np.linalg.norm(right) < 1e-6:
        right = np.array([0.0, -1.0, 0.0])
    right = right / np.linalg.norm(right)
    down = np.cross(forward, right)
    pose = np.eye(4)
    pose[:3, :3] = np.column_stack([right, down, forward])
    pose[:3, 3] = position
    return pose


def sim_capture(world, channel, quality=85, view=None):
    after = time.monotonic()
    for _ in range(80):
        if view is not None:
            world.overview_frame(*view)
        latest = world.camera_capture(channel)
        if latest and latest[2] is not None and latest[0] >= after:
            at, pixels, geometry = latest
            info = {key: geometry[key] for key in ("width", "height", "fx", "fy", "cx", "cy")}
            return {"jpeg": encode(pixels, quality), "info": info, "world_from_camera": geometry["world_from_camera"], "depth_m": geometry["depth_m"], "at": at}
        time.sleep(0.05)
    return None


def usb_capture(camera, pose, fov_horizontal_deg):
    jpeg = camera.frame()
    if jpeg is None:
        return None
    from PIL import Image
    width, height = Image.open(io.BytesIO(jpeg)).size
    return {"jpeg": jpeg, "info": lens(width, height, fov_horizontal_deg), "world_from_camera": pose, "depth_m": None, "at": time.monotonic()}


def locate(capture, pixels, plane_z=None):
    info = capture["info"]
    if capture.get("world_from_camera") is None:
        raise ValueError("This camera's position is not set, so its pixels cannot be turned into positions; use a wrist camera for locate")
    pose = np.asarray(capture["world_from_camera"], dtype=float)
    points = []
    for u, v in pixels:
        u = float(u)
        v = float(v)
        if not (0 <= u < info["width"] and 0 <= v < info["height"]):
            raise ValueError(f"Pixel {u:.0f},{v:.0f} is outside the {info['width']}x{info['height']} image")
        ray = np.array([(u + 0.5 - info["cx"]) / info["fx"], (v + 0.5 - info["cy"]) / info["fy"], 1.0])
        if capture.get("depth_m") is not None and plane_z is None:
            depth = float(capture["depth_m"][int(v), int(u)])
            points.append({"xyz": (pose[:3, :3] @ (ray * depth) + pose[:3, 3]).round(4).tolist(), "method": "depth", "distance_m": round(depth, 4)})
            continue
        if plane_z is None:
            raise ValueError("This camera has no depth; pass plane_z, the height of the surface the point lies on")
        direction = pose[:3, :3] @ ray
        if abs(direction[2]) < 1e-6:
            raise ValueError("That pixel looks parallel to the plane")
        t = (float(plane_z) - pose[2, 3]) / direction[2]
        if t <= 0:
            raise ValueError("The plane is behind the camera for that pixel")
        points.append({"xyz": (pose[:3, 3] + direction * t).round(4).tolist(), "method": "plane", "distance_m": round(float(t * np.linalg.norm(direction)), 4)})
    return points
