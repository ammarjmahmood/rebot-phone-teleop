import numpy as np
import pytest
from rebot_teleop import tasks
from rebot_teleop.kinematics import Kinematics, mount_placement
from rebot_teleop.node import ASSETS, KEEP_OUT, MOUNTS
from rebot_teleop.rs_arm import LIMITS_DEG


def kinematics(name):
    kin = Kinematics(ASSETS / "rebot_rs.urdf", mount_placement(**MOUNTS[name]))
    kin.lower = np.radians([low for low, _ in LIMITS_DEG])
    kin.upper = np.radians([high for _, high in LIMITS_DEG])
    kin.keep_out = KEEP_OUT + [tasks.table_box()]
    return kin


def top_down(side, yaw=0.0):
    base = np.array([[0.0, -side, 0.0], [0.0, 0.0, side], [-1.0, 0.0, 0.0]])
    c, s = np.cos(np.radians(yaw)), np.sin(np.radians(yaw))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]) @ base


def reachable(kin, xyz, rotation, seed):
    q, residual = kin.inverse_pose(xyz, rotation, seed, orientation_weight=1.0, iterations=200)
    if residual > 0.005:
        return None
    try:
        kin.validate(q)
    except ValueError:
        return None
    return q


@pytest.mark.parametrize("task", list(tasks.TASKS))
def test_every_layout_is_reachable_from_above(task):
    arms = {"left": kinematics("left"), "right": kinematics("right")}
    for seed in range(40):
        spec = tasks.scene(task, seed)
        for item in spec["objects"]:
            side = 1.0 if item["xyz"][1] > 0 else -1.0
            name = "left" if side > 0 else "right"
            ready = np.radians(tasks.READY_DEG[name])
            for height in (0.015, 0.09):
                point = [item["xyz"][0], item["xyz"][1], tasks.TABLE["top"] + height]
                assert reachable(arms[name], point, top_down(side), ready) is not None, f"{task} seed {seed} {item['name']} at {point}"


def test_ready_pose_clears_the_table_and_routes_from_home():
    for name in ("left", "right"):
        kin = kinematics(name)
        ready = np.radians(tasks.READY_DEG[name])
        kin.validate(ready)
        home = np.radians([-90.0, 170.0, 145.0, 30.0, 0.0, 0.0]) if name == "left" else np.radians([90.0, 170.0, 145.0, 30.0, 0.0, 0.0])
        assert len(kin.route(home, ready)) >= 1
        assert len(kin.route(ready, home)) >= 1


def test_checks_score_layouts_as_not_done():
    for task in tasks.TASKS:
        spec = tasks.scene(task, 0)
        states = {"objects": {item["name"]: {"xyz": item["xyz"], "upright": True} for item in spec["objects"]}}
        ok, detail = tasks.check(task, states)
        assert not ok, f"{task} counts as solved before anything moves: {detail}"
