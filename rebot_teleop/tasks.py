import math
import numpy as np

TABLE = {"top": 0.68, "x": [0.12, 0.55], "y": [-0.40, 0.40]}
BLOCK = 0.03
BIN = 0.11
RED = ([0.85, 0.12, 0.1, 1.0], "red")
BLUE = ([0.1, 0.3, 0.85, 1.0], "blue")
GREEN = ([0.15, 0.65, 0.2, 1.0], "green")
YELLOW = ([0.9, 0.75, 0.1, 1.0], "yellow")
ZONE_X = (0.24, 0.36)
ZONE_Y = (0.16, 0.32)
READY_DEG = {
    "left": [-28.5, 107.4, 43.5, 74.3, 61.2, -9.1],
    "right": [28.5, 107.3, 43.3, 74.4, -61.1, 9.1],
}


def table_box(table=TABLE, finger_clearance=0.004):
    center = np.array([(table["x"][0] + table["x"][1]) / 2, (table["y"][0] + table["y"][1]) / 2, table["top"] / 2])
    half = np.array([(table["x"][1] - table["x"][0]) / 2, (table["y"][1] - table["y"][0]) / 2, table["top"] / 2])
    return ("table", center, half, finger_clearance)


def block(name, color, x, y, yaw=0.0, size=BLOCK):
    return {"name": name, "kind": "block", "size": size, "xyz": [x, y, TABLE["top"] + size / 2 + 0.001], "yaw_deg": yaw, "rgba": color[0], "color": color[1], "mass": 0.04}


def bin_(name, color, x, y, yaw=0.0):
    return {"name": name, "kind": "bin", "size": BIN, "height": 0.045, "wall": 0.006, "xyz": [x, y, TABLE["top"] + 0.001], "yaw_deg": yaw, "rgba": color[0], "color": color[1], "mass": 0.3}


def spots(rng, side, count, gap):
    for _ in range(200):
        found = []
        for _ in range(500):
            x = rng.uniform(*ZONE_X)
            y = side * rng.uniform(*ZONE_Y)
            if all(math.hypot(x - a, y - b) >= gap for a, b in found):
                found.append((x, y))
                if len(found) == count:
                    return found
    raise ValueError(f"Could not fit {count} objects {gap} m apart in the reachable zone")


def lift_block(rng):
    side = rng.choice([1, -1])
    (x, y), = spots(rng, side, 1, 0.0)
    return [block("red_block", RED, x, y, rng.uniform(-40, 40))]


def block_in_bin(rng):
    side = rng.choice([1, -1])
    (bx, by), (cx, cy) = spots(rng, side, 2, 0.10)
    return [block("red_block", RED, bx, by, rng.uniform(-40, 40)), bin_("blue_bin", BLUE, cx, cy)]


def stack(rng):
    side = rng.choice([1, -1])
    (ax, ay), (bx, by) = spots(rng, side, 2, 0.08)
    return [block("red_block", RED, ax, ay, rng.uniform(-40, 40)), block("blue_block", BLUE, bx, by, rng.uniform(-40, 40))]


def sort_colors(rng):
    left = spots(rng, 1, 2, 0.10)
    right = spots(rng, -1, 2, 0.10)
    order = rng.permutation(2)
    blocks = [block("red_block", RED, *left[order[0]], rng.uniform(-30, 30)), block("green_block", GREEN, *right[order[1]], rng.uniform(-30, 30))]
    return blocks + [bin_("red_bin", RED, *left[order[1]]), bin_("green_bin", GREEN, *right[order[0]])]


def lift_both(rng):
    (lx, ly), = spots(rng, 1, 1, 0.0)
    (rx, ry), = spots(rng, -1, 1, 0.0)
    return [block("yellow_block", YELLOW, lx, ly, rng.uniform(-30, 30)), block("blue_block", BLUE, rx, ry, rng.uniform(-30, 30))]


def inside(objects, item, container):
    a = np.array(objects[item]["xyz"])
    b = np.array(objects[container]["xyz"])
    return abs(a[0] - b[0]) < BIN / 2 and abs(a[1] - b[1]) < BIN / 2 and a[2] < TABLE["top"] + 0.05


def check_lift(objects):
    z = objects["red_block"]["xyz"][2] - TABLE["top"] - BLOCK / 2
    return z > 0.06, f"red block is {z * 100:.1f} cm above the table; needs 6 cm"


def check_bin(objects):
    ok = inside(objects, "red_block", "blue_bin")
    return ok, "red block is in the blue bin" if ok else "red block is not in the blue bin"


def check_stack(objects):
    red = np.array(objects["red_block"]["xyz"])
    blue = np.array(objects["blue_block"]["xyz"])
    offset = float(np.hypot(*(red[:2] - blue[:2])))
    ok = offset < 0.015 and abs(red[2] - blue[2] - BLOCK) < 0.008 and objects["red_block"]["upright"]
    return ok, f"red block is {offset * 100:.1f} cm off centre and {(red[2] - blue[2]) * 100:.1f} cm above the blue block"


def check_sort(objects):
    red = inside(objects, "red_block", "red_bin")
    green = inside(objects, "green_block", "green_bin")
    return red and green, f"red in red bin: {red}, green in green bin: {green}"


def check_lift_both(objects):
    yellow = objects["yellow_block"]["xyz"][2] - TABLE["top"] - BLOCK / 2
    blue = objects["blue_block"]["xyz"][2] - TABLE["top"] - BLOCK / 2
    return yellow > 0.06 and blue > 0.06, f"yellow block {yellow * 100:.1f} cm and blue block {blue * 100:.1f} cm above the table; both need 6 cm"


TASKS = {
    "lift_block": {"layout": lift_block, "check": check_lift, "arms": "one", "text": "Pick up the red block and hold it at least 6 cm above the table. Leave it held when the policy ends."},
    "block_in_bin": {"layout": block_in_bin, "check": check_bin, "arms": "one", "text": "Put the red block into the blue bin. Both are on the same side of the robot."},
    "stack": {"layout": stack, "check": check_stack, "arms": "one", "text": "Stack the red block on top of the blue block, centred and upright."},
    "sort_colors": {"layout": sort_colors, "check": check_sort, "arms": "two", "text": "Put the red block in the red bin and the green block in the green bin. Each arm works on its own side."},
    "lift_both": {"layout": lift_both, "check": check_lift_both, "arms": "two", "text": "Pick up the yellow block with the left arm and the blue block with the right arm, moving both arms at the same time, and hold both at least 6 cm above the table."},
}


def scene(name, seed):
    if name not in TASKS:
        raise ValueError(f"Unknown task {name}; tasks are {', '.join(TASKS)}")
    rng = np.random.default_rng(seed)
    return {"table": dict(TABLE), "objects": TASKS[name]["layout"](rng)}


def check(name, states):
    objects = states["objects"]
    ok, detail = TASKS[name]["check"](objects)
    return bool(ok), detail
