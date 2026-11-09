import math
import numpy as np
import pytest
from rebot_teleop.kinematics import Kinematics, mount_placement
from rebot_teleop.node import ASSETS, HOMES_DEG, KEEP_OUT, MOUNTS
from rebot_teleop.policy import ArmView, CommandError, plan
from rebot_teleop.rs_arm import LIMITS_DEG


def arms(poses=None):
    views = {}
    for name in ("left", "right"):
        kin = Kinematics(ASSETS / "rebot_rs.urdf", mount_placement(**MOUNTS[name]))
        kin.lower = np.radians([low for low, _ in LIMITS_DEG])
        kin.upper = np.radians([high for _, high in LIMITS_DEG])
        kin.keep_out = KEEP_OUT
        home = np.radians(HOMES_DEG[name])
        views[name] = ArmView(name, kin, np.radians((poses or {}).get(name, HOMES_DEG[name])), home)
    return views


def tip_change(view, move):
    return view.kin.pose(move.q)[0] - view.kin.pose(view.q)[0]


def test_raise_hand_uses_joint_one_and_lifts_both_mirrored_arms():
    views = arms()
    step = plan("raise hands", views)[0]
    for name, move in step.items():
        changed = np.flatnonzero(np.abs(move.q - views[name].q) > 1e-6)
        assert changed.tolist() == [0]
        shift = tip_change(views[name], move)
        assert shift[0] > 0.1 and shift[2] > 0
    assert step["left"].q[0] - views["left"].q[0] == pytest.approx(-(step["right"].q[0] - views["right"].q[0]))


def test_side_words_pick_arms_and_lower_undoes_raise():
    views = arms()
    raised = plan("raise left hand by 30 degrees", views)
    assert list(raised[0]) == ["left"]
    assert math.degrees(raised[0]["left"].q[0] - views["left"].q[0]) == pytest.approx(30.0)
    views["left"].q = raised[0]["left"].q
    lowered = plan("lower left hand by 30 degrees", views)[0]["left"]
    assert np.allclose(lowered.q, np.radians(HOMES_DEG["left"]))


def test_elbow_and_wrist_commands_pick_their_joints():
    views = arms()
    elbow = plan("raise right elbow", views)[0]["right"]
    assert np.flatnonzero(np.abs(elbow.q - views["right"].q) > 1e-6).tolist() == [1]
    assert views["right"].kin.points(elbow.q)[3][2] > views["right"].kin.points(views["right"].q)[3][2] - 1e-6
    wrist = plan("raise left wrist", views)[0]["left"]
    assert set(np.flatnonzero(np.abs(wrist.q - views["left"].q) > 1e-6).tolist()) <= {3, 4}
    twist = plan("twist left wrist clockwise then twist left wrist counterclockwise", views)
    assert twist[0]["left"].q[5] != pytest.approx(views["left"].q[5])
    assert twist[1]["left"].q[5] == pytest.approx(views["left"].q[5])


def test_cartesian_move_and_gripper():
    views = arms({"left": [-60.0, 120.0, 100.0, 30.0, 0.0, 0.0], "right": [60.0, 120.0, 100.0, 30.0, 0.0, 0.0]})
    step = plan("move hands forward 4 cm", views)[0]
    for name, move in step.items():
        assert np.allclose(tip_change(views[name], move), [0.04, 0.0, 0.0], atol=0.003)
    grip = plan("open grippers then close right gripper", views)
    assert grip[0]["left"].width == pytest.approx(0.05) and grip[1]["right"].width == 0.0


def test_moves_into_the_post_are_refused():
    views = arms({"left": [-90.0, 170.0, 125.0, 30.0, 0.0, 0.0], "right": [90.0, 170.0, 145.0, 30.0, 0.0, 0.0]})
    with pytest.raises(ValueError, match="post"):
        views["left"].kin.validate(views["left"].q)
    views = arms()
    with pytest.raises(CommandError, match="post"):
        plan("bend left elbow by 25 degrees", views)


def test_unknown_words_explain_what_works():
    with pytest.raises(CommandError, match="raise left hand"):
        plan("do a backflip", arms())
