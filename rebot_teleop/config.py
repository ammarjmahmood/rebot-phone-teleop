import json
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
URDF = ASSETS / "rebot_rs.urdf"
DATA = Path(os.environ.get("REBOT_DATA", Path.home() / ".rebot-teleop"))
STATIC = Path(__file__).resolve().parent / "static"

HTTP_PORT = int(os.environ.get("REBOT_PORT", "8081"))
HTTPS_PORT = int(os.environ.get("REBOT_HTTPS_PORT", "8443"))

MOTORS = tuple((i, "rs-06" if i <= 3 else "rs-00") for i in range(1, 8))
HOST_ID = 0xFD
PEAK_NM = {"rs-06": 36.0, "rs-00": 14.0, "rs-03": 60.0}
RATED_NM = {"rs-06": 11.0, "rs-00": 5.0, "rs-03": 20.0}

SEEED_LIMITS_DEG = ((-160.4, 160.4), (0.0, 179.9), (0.0, 179.9), (-90.0, 90.0), (-90.0, 90.0), (-179.9, 179.9))
LIMITS_DEG = ((-158.0, 158.0), (-60.0, 240.0), (-3.0, 178.0), (-88.0, 88.0), (-88.0, 88.0), (-179.0, 179.0))
LIMIT_SLACK_DEG = 20.0

GAINS = ((120.0, 4.0), (110.0, 4.0), (100.0, 4.0), (40.0, 1.5), (35.0, 1.2), (30.0, 1.0), (12.0, 0.05))

LOOP_HZ = 200.0
SERVO_TAU_S = 0.025
SERVO_MAX_DEG_S = (240.0, 240.0, 270.0, 360.0, 360.0, 420.0)
SERVO_MAX_DEG_S2 = 3600.0
MOVE_DEG_S = 90.0
MOVE_DEG_S2 = 360.0
GRIPPER_DEG_S = 400.0
GRIPPER_OPEN_DEG = 270.0
GRIPPER_CLOSED_DEG = 0.0
GRIPPER_WIDTH_M = 0.05
GRIPPER_LEAD_DEG = 18.0
GRIPPER_HOLD_LEAD_DEG = 5.0

TEMPERATURE_WARN_C = float(os.environ.get("REBOT_TELEOP_TEMPERATURE_WARN", "75"))
TEMPERATURE_LIMIT_C = float(os.environ.get("REBOT_TELEOP_TEMPERATURE_LIMIT", "95"))
COOLED_C = TEMPERATURE_LIMIT_C - 10.0
COOL_HOME_DEG_S = 30.0
FEEDBACK_TIMEOUT_S = 0.6
PUSH_FRACTION = 0.85
BLOCKED_AFTER_S = 0.5
TORQUE_HISTORY_S = 60.0
HEAT_WARN = 0.5
HEAT_LOWER = 0.8
HEAT_LOWER_HEAVY = 0.95
HEAT_PLAN_LIMIT = 0.7
HEAT_RESUME = 0.3
HEAT_HOLD_MARGIN = 0.85
HEAT_COOL_S = 300.0
HEAT_MOVING_DEG_S = 20.0
HEAT_MODEL_MARGIN = 1.15
REST_TIMEOUT_S = 600.0
MOTORLOG_HZ = 5.0
REACH_GUARD_AT = 0.5
TABLE_DEFAULT = {"top": 0.68, "front": None, "clearance": 0.01}
TABLE_POINTS = (("gripper_end", 0.0), ("gripper_left", 0.01), ("gripper_right", 0.01), ("link6", 0.04), ("link5", 0.04), ("link4", 0.04))
MOTORLOG_KEEP_DAYS = 14
REST_COMES_BACK = False
RELIEF_X = 0.12
RELIEF_STOP_FRACTION = 0.6
RELIEF_ABOVE_TABLE = 0.08
REST_RETURN_DEG_S = 30.0
REST_SETTLE_S = 0.5
CATCH_LIMIT = 3
CATCH_WINDOW_S = 20.0
TORQUE_BUCKET_S = 0.1

POST_TOP_M = 1.30
POST_W_M = 0.040
SHOULDER_BELOW_TOP_M = 0.155
MOUNTS = {
    "left": {"xyz": [0.0, POST_W_M / 2, POST_TOP_M - SHOULDER_BELOW_TOP_M], "rpy_deg": [-90.0, 0.0, 0.0]},
    "right": {"xyz": [0.0, -POST_W_M / 2, POST_TOP_M - SHOULDER_BELOW_TOP_M], "rpy_deg": [90.0, 0.0, 0.0]},
}
POSES_DEG = {
    "home": {"left": [-90.0, 170.0, 145.0, 14.0, 5.0, 88.0], "right": [90.0, 170.0, 145.0, 16.0, -5.0, -101.0]},
    "ready": {"left": [-28.5, 107.4, 43.5, 74.3, 61.2, -9.1], "right": [28.5, 107.3, 43.3, 74.4, -61.1, 9.1]},
}
DEFAULT_CAN = {"left": "can1", "right": "can2"}
DEFAULT_CAMERAS = {
    "left": "/dev/v4l/by-id/usb-Innomaker_Innomaker-U20CAM-720P_SN0001-video-index0",
    "right": "/dev/v4l/by-id/usb-Innomaker_Innomaker-U20CAM-1080p-S1_SN0001-video-index0",
    "head": "hp60c",
}
HEAD_POSE = {"xyz": [0.06, 0.0, 1.486], "look_at": [0.846, 0.0, 0.70]}
LIFT_PORT = "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_df6470a31b703c2f-if00"
ANGSTROM_SDK = ROOT / "vendor" / "angstrong"


def load(name, default):
    path = DATA / name
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(name, value):
    path = DATA / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1))
    tmp.replace(path)


def radians(values):
    return [math.radians(v) for v in values]
