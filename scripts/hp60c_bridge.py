import argparse
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image


def write_atomic(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


class Bridge(Node):
    def __init__(self, namespace, out, rotate, rate_hz):
        super().__init__("rebot_hp60c_bridge")
        self.out = out
        self.rotate = rotate
        self.period = 1.0 / rate_hz
        self.info = None
        self.depth = None
        self.last = 0.0
        base = f"/{namespace}/camera_publisher/"
        self.create_subscription(CameraInfo, base + "rgb0/camera_info", self.on_info, qos_profile_sensor_data)
        self.create_subscription(Image, base + "depth0/image_raw", self.on_depth, qos_profile_sensor_data)
        self.create_subscription(Image, base + "rgb0/image", self.on_color, qos_profile_sensor_data)

    def turn(self, image):
        if self.rotate == 180:
            return cv2.rotate(image, cv2.ROTATE_180)
        return image

    def on_info(self, message):
        self.info = message

    def on_depth(self, message):
        if message.encoding not in ("16UC1", "mono16"):
            return
        self.depth = np.frombuffer(bytes(message.data), dtype=np.uint16).reshape(message.height, message.width)

    def on_color(self, message):
        now = time.monotonic()
        if now - self.last < self.period or self.info is None:
            return
        self.last = now
        pixels = np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(message.height, message.width, -1)
        if message.encoding == "rgb8":
            pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
        width, height = message.width, message.height
        fx, fy, cx, cy = self.info.k[0], self.info.k[4], self.info.k[2], self.info.k[5]
        if self.rotate == 180:
            cx, cy = width - 1 - cx, height - 1 - cy
        ok, jpeg = cv2.imencode(".jpg", self.turn(pixels), [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return
        if self.depth is not None and self.depth.shape == (height, width):
            buffer = self.turn(self.depth).astype("<u2").tobytes()
            write_atomic(self.out / "depth.raw", buffer)
        write_atomic(self.out / "color.jpg", jpeg.tobytes())
        meta = {"width": width, "height": height, "fx": fx, "fy": fy, "cx": cx, "cy": cy, "depth": self.depth is not None, "depth_units_m": 0.001, "time": time.time()}
        write_atomic(self.out / "info.json", json.dumps(meta).encode())


def main():
    parser = argparse.ArgumentParser(description="Copy the Angstrong HP60C colour image, aligned depth and lens into a folder the teleop server reads as a camera")
    parser.add_argument("--namespace", default="ascamera_hp60c")
    parser.add_argument("--out", default="/dev/shm/rebot-torso")
    parser.add_argument("--rotate", type=int, default=180, choices=[0, 180])
    parser.add_argument("--rate", type=float, default=10.0)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    node = Bridge(args.namespace, out, args.rotate, args.rate)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
