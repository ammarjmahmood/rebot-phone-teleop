import argparse
import json
import select
import sys
import time
import hebi
from hebi.util import create_mobile_io


LABELS = {1: "Move", 2: "Open", 3: "Close", 8: "Home"}
AXES = {3: "Grip"}


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read_inputs(feedback):
    inputs = {}
    io = getattr(feedback, "io", None)
    if io is None:
        return inputs
    for channel in range(1, 9):
        if io.a.has_float(channel):
            inputs[f"a{channel}"] = float(io.a.get_float(channel))
        if io.b.has_int(channel):
            inputs[f"b{channel}"] = int(io.b.get_int(channel))
    return inputs


def prepare(phone):
    try:
        phone.resetUI()
        for button, label in LABELS.items():
            phone.set_button_label(button, label)
        for axis, label in AXES.items():
            phone.set_axis_label(axis, label)
        phone.set_snap(3, float("nan"))
        phone.clear_text()
        phone.add_text("reBot teleop: hold Move and the gripper follows the phone.")
    except Exception as error:
        emit({"status": "connected", "warning": f"Labels not set: {error}"})


def handle_commands(phone):
    while select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            command = json.loads(line)
        except ValueError:
            continue
        try:
            if command.get("vibrate"):
                phone.send_vibrate()
            if "text" in command:
                phone.clear_text()
                phone.add_text(str(command["text"])[:200])
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default="HEBI")
    parser.add_argument("--name", default="mobileIO")
    parser.add_argument("--address", action="append", default=[])
    args = parser.parse_args()
    lookup = hebi.Lookup(args.address + ["255.255.255.255"]) if args.address else hebi.Lookup()
    phone = None
    while phone is None:
        time.sleep(1.0)
        phone = create_mobile_io(lookup, args.family, args.name)
        if phone is None:
            emit({"status": "searching"})
    prepare(phone)
    emit({"status": "connected"})
    while True:
        handle_commands(phone)
        if not phone.update(timeout_ms=200):
            emit({"status": "silent"})
            continue
        module = phone.get_last_feedback()
        position = getattr(module, "ar_position", None)
        orientation = getattr(module, "ar_orientation", None)
        if position is None or orientation is None:
            continue
        values = [float(v) for v in list(position) + list(orientation)]
        if not all(v == v for v in values):
            continue
        emit({"status": "pose", "position": values[:3], "quaternion_wxyz": values[3:], "inputs": read_inputs(module)})


if __name__ == "__main__":
    main()
