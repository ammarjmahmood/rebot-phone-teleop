import math
import re
from dataclasses import dataclass
import numpy as np
import pinocchio as pin


DEFAULT_DEG = 20.0
SMALL_DEG = 10.0
LARGE_DEG = 45.0
DEFAULT_CM = 5.0
SMALL_CM = 2.0
LARGE_CM = 10.0
WIDTH_OPEN = 0.05
MARGIN = math.radians(1.0)
MAX_CARTESIAN_SWING = math.radians(60.0)
PARTS = {
    "hand": "hand", "hands": "hand", "arm": "hand", "arms": "hand", "shoulder": "hand", "shoulders": "hand",
    "elbow": "elbow", "elbows": "elbow", "forearm": "elbow", "forearms": "elbow",
    "wrist": "wrist", "wrists": "wrist",
    "gripper": "gripper", "grippers": "gripper", "claw": "gripper", "claws": "gripper", "fingers": "gripper",
}
DIRECTIONS = {
    "forward": "forward", "forwards": "forward", "ahead": "forward", "front": "forward",
    "back": "back", "backward": "back", "backwards": "back", "behind": "back",
    "up": "up", "upward": "up", "upwards": "up", "higher": "up",
    "down": "down", "downward": "down", "downwards": "down", "lower": "down",
    "left": "left", "right": "right",
    "out": "out", "outward": "out", "outwards": "out", "away": "out", "apart": "out", "wider": "out",
    "in": "in", "inward": "in", "inwards": "in", "closer": "in", "together": "in",
}
HELP = "Try: get ready, raise left hand, lower hands, raise elbows, bend right elbow, straighten elbows, raise wrists, turn wrists out, twist left wrist clockwise, move hands forward 5 cm, open grippers, close right gripper, joint 3 by 10, wave, go home"


class CommandError(ValueError):
    pass


@dataclass
class ArmView:
    name: str
    kin: object
    q: np.ndarray
    home: np.ndarray

    @property
    def side(self):
        return 1.0 if float(self.kin.mount.translation[1]) >= 0 else -1.0


@dataclass
class Move:
    arm: str
    q: np.ndarray
    width: float | None
    note: str


def clauses(text):
    text = text.lower().replace("°", " degrees ").replace("-", " minus ")
    text = re.sub(r"counter ?clockwise|anti ?clockwise", "counterclockwise", text)
    parts = re.split(r"\s*(?:,|;|\.(?!\d)|\band then\b|\bthen\b|\bafter that\b|\band\b)\s*", text)
    return [part.strip() for part in parts if part and part.strip()]


def pick_arms(words, names):
    chosen = []
    rest = []
    for index, word in enumerate(words):
        following = [w for w in words[index + 1:index + 3] if w != "the"]
        if word in ("left", "right") and word in names and (following[:1] and following[0] in PARTS or index == 0):
            if word not in chosen:
                chosen.append(word)
            continue
        rest.append(word)
    if "both" in words or not chosen:
        chosen = list(names)
    return chosen, [w for w in rest if w != "both"]


def amount_deg(text, default=DEFAULT_DEG):
    match = re.search(r"(minus\s+)?(\d+(?:\.\d+)?)\s*(?:deg|degs|degree|degrees)\b", text)
    if match:
        return float(match.group(2)) * (-1 if match.group(1) else 1)
    if re.search(r"\ball the way\b", text):
        return 360.0
    if re.search(r"\b(a little|a bit|slightly|a touch|tiny)\b", text):
        return SMALL_DEG
    if re.search(r"\b(a lot|way|far|big)\b", text):
        return LARGE_DEG
    if not re.search(r"\d\s*(cm|mm|centimet|millimet|m\b|meter|metre|inch|in\b)", text):
        match = re.search(r"(minus\s+)?\b(\d+(?:\.\d+)?)\b", text)
        if match:
            return float(match.group(2)) * (-1 if match.group(1) else 1)
    return default


def amount_m(text):
    match = re.search(r"(\d+(?:\.\d+)?)\s*(cm|centimet\w*|mm|millimet\w*|m|meters?|metres?|inch\w*|in)\b", text)
    if match:
        value = float(match.group(1))
        unit = match.group(2)
        scale = 0.01 if unit.startswith("c") else 0.001 if unit.startswith("mm") or unit.startswith("milli") else 0.0254 if unit.startswith("in") else 1.0
        return value * scale
    if re.search(r"\b(a little|a bit|slightly|a touch|tiny)\b", text):
        return SMALL_CM / 100
    if re.search(r"\b(a lot|way|far|big)\b", text):
        return LARGE_CM / 100
    return DEFAULT_CM / 100


def points(view, q):
    return view.kin.points(q)


def slope_of(metric, q, joint):
    eps = 1e-3
    up = q.copy()
    up[joint] += eps
    down = q.copy()
    down[joint] -= eps
    return metric(up) - metric(down)


def toward(view, metric, q, joint):
    slope = slope_of(metric, q, joint)
    if abs(slope) < 1e-6:
        raise CommandError(f"{view.name} J{joint + 1} does not change that from this pose")
    return 1.0 if slope > 0 else -1.0


def best(view, metric, q, joints):
    joint = max(joints, key=lambda j: abs(slope_of(metric, q, j)))
    return joint, toward(view, metric, q, joint)


def turn(view, q, joint, delta, what):
    lower = view.kin.lower[joint] + MARGIN
    upper = view.kin.upper[joint] - MARGIN
    goal = q.copy()
    goal[joint] = float(np.clip(q[joint] + delta, lower, upper))
    if abs(goal[joint] - q[joint]) < math.radians(1.0):
        raise CommandError(f"{view.name} {what}: J{joint + 1} is already at its limit")
    try:
        view.kin.validate(goal)
    except ValueError as error:
        raise CommandError(f"{view.name} {what}: {error}") from None
    limited = abs(goal[joint] - q[joint]) < abs(delta) - 1e-6
    note = f"{view.name} {what}: J{joint + 1} {math.degrees(q[joint]):.0f} to {math.degrees(goal[joint]):.0f} deg" + (" (stopped at the joint limit)" if limited else "")
    return goal, note


def axis_of(view, q, joint):
    eps = 1e-4
    moved = q.copy()
    moved[joint] += eps
    _, before = view.kin.pose(q)
    _, after = view.kin.pose(moved)
    return pin.log3(after @ before.T) / eps


def direction_vector(view, word):
    vectors = {"forward": [1, 0, 0], "back": [-1, 0, 0], "up": [0, 0, 1], "down": [0, 0, -1], "left": [0, 1, 0], "right": [0, -1, 0], "out": [0, view.side, 0], "in": [0, -view.side, 0]}
    return np.array(vectors[word], dtype=float)


def reach(view, q, word, distance):
    position, rotation = view.kin.pose(q)
    target = position + direction_vector(view, word) * distance
    solution, residual = view.kin.inverse_pose(target, rotation, q)
    if residual > 0.005 or np.max(np.abs(solution - q)) > MAX_CARTESIAN_SWING:
        raise CommandError(f"{view.name} hand cannot reach {distance * 100:.0f} cm {word} from here")
    try:
        view.kin.validate(solution)
    except ValueError as error:
        raise CommandError(f"{view.name} hand {word}: {error}") from None
    return solution, f"{view.name} hand {word} {distance * 100:.0f} cm"


def joint_move(view, q, text):
    match = re.search(r"\b(?:j|joint)\s*([1-6])\b(.*)", text)
    joint = int(match.group(1)) - 1
    tail = match.group(2)
    absolute = re.search(r"\b(?:to|at)\s+(minus\s+)?(\d+(?:\.\d+)?)", tail)
    if absolute:
        target = math.radians(float(absolute.group(2)) * (-1 if absolute.group(1) else 1))
        return turn(view, q, joint, target - q[joint], f"joint {joint + 1}")
    value = amount_deg(tail)
    if re.search(r"\b(down|back|negative|decrease)\b", tail):
        value = -abs(value)
    return turn(view, q, joint, math.radians(value), f"joint {joint + 1}")


def single(view, q, text, words):
    part = next((PARTS[w] for w in words if w in PARTS), None)
    direction = next((DIRECTIONS[w] for w in words if w in DIRECTIONS), None)
    has_length = bool(re.search(r"\d\s*(cm|mm|centimet|millimet|m\b|meter|metre|inch|in\b)", text))
    verbs = set(words)
    if verbs & {"home", "rest", "starting", "start"}:
        return view.home.copy(), None, f"{view.name} home"
    if "ready" in verbs:
        from rebot_teleop.tasks import READY_DEG
        if view.name not in READY_DEG:
            raise CommandError(f"No ready pose for {view.name}")
        return np.radians(READY_DEG[view.name]), None, f"{view.name} ready"
    if re.search(r"\b(?:j|joint)\s*[1-6]\b", text):
        goal, note = joint_move(view, q, text)
        return goal, None, note
    if verbs & {"open", "release"} and part in (None, "gripper", "hand"):
        width = WIDTH_OPEN / 2 if "half" in verbs else WIDTH_OPEN
        return q.copy(), width, f"{view.name} gripper open"
    if verbs & {"close", "grab", "grip", "grasp", "pinch", "squeeze"}:
        return q.copy(), 0.0, f"{view.name} gripper close"
    angle = math.radians(abs(amount_deg(text)))
    if verbs & {"twist", "rotate", "roll", "spin"} or "clockwise" in verbs or "counterclockwise" in verbs:
        points_now = points(view, q)
        pointing = points_now[7] - points_now[6]
        if np.linalg.norm(pointing) < 1e-3:
            pointing = view.kin.pose(q)[1][:, 0]
        facing = 1.0 if axis_of(view, q, 5) @ pointing >= 0 else -1.0
        sense = -1.0 if "counterclockwise" in verbs or "left" in verbs else 1.0
        amount = math.radians(abs(amount_deg(text, 30.0)))
        label = "counterclockwise" if sense < 0 else "clockwise"
        goal, note = turn(view, q, 5, sense * facing * amount, f"twist wrist {label}")
        return goal, None, note
    if verbs & {"bend", "flex", "curl", "fold"} or verbs & {"straighten", "extend", "unbend", "stretch"}:
        bend = bool(verbs & {"bend", "flex", "curl", "fold"})
        metric = lambda x: -np.linalg.norm(points(view, x)[7] - points(view, x)[2])
        sign = toward(view, metric, q, 2) * (1 if bend else -1)
        goal, note = turn(view, q, 2, sign * angle, "bend elbow" if bend else "straighten elbow")
        return goal, None, note
    moving = bool(verbs & {"move", "go", "push", "pull", "slide", "shift", "reach", "bring", "put"})
    if direction and (has_length or moving) and part in (None, "hand", "gripper"):
        goal, note = reach(view, q, direction, amount_m(text))
        return goal, None, note
    raising = bool(verbs & {"raise", "lift", "up", "higher"}) or direction == "up"
    lowering = bool(verbs & {"lower", "drop", "down"}) or direction == "down"
    if raising or lowering:
        sense = 1.0 if raising and not lowering else -1.0
        if part == "elbow":
            metric = lambda x: points(view, x)[3][2] + 0.3 * view.side * points(view, x)[3][1]
            joint, what = 1, "elbow"
            sign = toward(view, metric, q, joint)
        elif part == "wrist" or part == "gripper":
            metric = lambda x: points(view, x)[7][2] + 0.3 * points(view, x)[7][0]
            joint, sign = best(view, metric, q, (3, 4))
            what = "wrist"
        else:
            metric = lambda x: points(view, x)[7][2] + 0.3 * points(view, x)[7][0]
            joint, what = 0, "hand"
            sign = toward(view, metric, q, joint)
        sign *= sense
        goal, note = turn(view, q, joint, sign * angle, ("raise " if sense > 0 else "lower ") + what)
        return goal, None, note
    if part in ("wrist", "gripper", "hand") and direction in ("in", "out", "left", "right") and verbs & {"turn", "tilt", "point", "swing", "aim"}:
        outward = {"out": 1.0, "in": -1.0, "left": view.side, "right": -view.side}[direction]
        metric = lambda x: view.side * points(view, x)[7][1]
        joint, sign = best(view, metric, q, (4, 3))
        goal, note = turn(view, q, joint, sign * outward * angle, f"turn wrist {direction}")
        return goal, None, note
    raise CommandError(f"I did not understand \"{text}\". {HELP}")


def wave(view, q):
    steps = []
    amount = math.radians(25.0)
    for offset in (amount, -amount, amount, 0.0):
        goal = q.copy()
        goal[4] = float(np.clip(q[4] + offset, view.kin.lower[4] + MARGIN, view.kin.upper[4] - MARGIN))
        steps.append(Move(view.name, goal, None, f"{view.name} wave"))
    return steps


def plan(text, arms):
    if not isinstance(text, str) or not text.strip() or len(text) > 300:
        raise CommandError("Type a short command. " + HELP)
    current = {name: np.asarray(view.q, dtype=float).copy() for name, view in arms.items()}
    steps = []
    for clause in clauses(text):
        words = re.findall(r"[a-z]+|\d+(?:\.\d+)?", clause)
        names, rest = pick_arms(words, list(arms))
        body = " ".join(rest)
        if "wave" in words:
            sequences = {name: wave(arms[name], current[name]) for name in names}
            for index in range(4):
                steps.append({name: sequences[name][index] for name in names})
            continue
        step = {}
        for name in names:
            view = arms[name]
            goal, width, note = single(view, current[name], body, rest)
            step[name] = Move(name, goal, width, note)
            current[name] = goal
        steps.append(step)
    if not steps:
        raise CommandError(HELP)
    return steps


def finite(values, count, what):
    array = np.asarray(values, dtype=float)
    if array.shape != (count,) or not np.isfinite(array).all():
        raise CommandError(f"{what} needs {count} finite numbers")
    return array


def target(view, spec):
    if not isinstance(spec, dict):
        raise CommandError(f"Describe the {view.name} move as an object")
    q = np.asarray(view.q, dtype=float).copy()
    notes = []
    if spec.get("joints_deg") is not None:
        q = np.radians(finite(spec["joints_deg"], 6, "joints_deg"))
        notes.append("joints " + " ".join(f"{v:.0f}" for v in np.degrees(q)))
    if spec.get("tip_xyz") is not None or spec.get("tip_delta") is not None:
        position, rotation = view.kin.pose(q)
        if spec.get("tip_rotation") is not None:
            rotation = np.asarray(spec["tip_rotation"], dtype=float)
            if rotation.shape != (3, 3) or not np.isfinite(rotation).all() or abs(np.linalg.det(rotation) - 1.0) > 0.05:
                raise CommandError("tip_rotation must be a 3 by 3 rotation matrix")
            u, _, vt = np.linalg.svd(rotation)
            rotation = u @ vt
        goal = finite(spec["tip_xyz"], 3, "tip_xyz") if spec.get("tip_xyz") is not None else position + finite(spec["tip_delta"], 3, "tip_delta")
        weight = float(spec.get("orientation_weight", 0.4))
        solution, residual = view.kin.inverse_pose(goal, rotation, q, orientation_weight=weight, iterations=150)
        tilt = ""
        if residual > 0.005 and spec.get("tip_rotation") is None:
            for looser in (0.05, 0.0):
                solution, residual = view.kin.inverse_pose(goal, rotation, q, orientation_weight=looser, iterations=300)
                if residual <= 0.005:
                    turned = math.degrees(np.linalg.norm(pin.log3(view.kin.pose(solution)[1] @ rotation.T)))
                    tilt = f", hand tilted {turned:.0f} deg to get there"
                    break
        if residual > 0.005:
            raise CommandError(f"{view.name} hand cannot reach {np.round(goal, 3).tolist()} (misses by {residual * 100:.1f} cm)")
        if np.max(np.abs(solution - q)) > math.radians(120.0):
            raise CommandError(f"{view.name} hand would swing more than 120 degrees to reach that point; go there in smaller steps")
        q = solution
        notes.append("tip to " + " ".join(f"{v:.3f}" for v in goal) + tilt)
    try:
        view.kin.validate(q)
    except ValueError as error:
        raise CommandError(f"{view.name}: {error}") from None
    width = None
    if spec.get("gripper_m") is not None:
        width = float(np.clip(float(spec["gripper_m"]), 0.0, WIDTH_OPEN))
        notes.append(f"gripper {width * 100:.1f} cm")
    if not notes:
        raise CommandError("Give joints_deg, tip_xyz, tip_delta or gripper_m")
    return Move(view.name, q, width, f"{view.name} " + ", ".join(notes))
