import numpy as np
import pytest
from rebot_teleop.controller import TeleopController
from rebot_teleop.kinematics import GRAVITY_MS2, MOUNT_PRESETS, Kinematics, parse_mount, rpy_matrix
from rebot_teleop.node import ASSETS, TeleopNode
from rebot_teleop.rs_arm import RSArm
from rebot_teleop.sources import PushSource


URDF = ASSETS / "rebot_rs.urdf"
POSES = ([0.0] * 6, [0.0, 0.6, 0.0, 0.0, 0.0, 0.0], [0.3, 1.2, -0.5, 0.2, 0.4, 0.0])


class StubBus:
    def __init__(self, channel):
        self.channel = channel

    def close(self):
        pass


def make_node(path, mount=None):
    arm = RSArm("can0", controller_factory=StubBus, gripper_open_deg=270.0)
    node = TeleopNode(path, arm=arm, mount=mount)
    node.controllers = {"quest": TeleopController(node, "quest", PushSource(), "webxr")}
    return node


def test_upright_is_the_default():
    kin = Kinematics(URDF)
    assert np.allclose(kin.mount, np.eye(3))
    assert np.allclose(kin.model.gravity.linear, [0.0, 0.0, -GRAVITY_MS2])


def test_presets_place_the_base_z_axis():
    left, _ = parse_mount("side_left")
    right, _ = parse_mount("side_right")
    assert np.allclose(left @ [0.0, 0.0, 1.0], [0.0, 1.0, 0.0], atol=1e-12)
    assert np.allclose(right @ [0.0, 0.0, 1.0], [0.0, -1.0, 0.0], atol=1e-12)


def test_angles_match_the_named_preset():
    explicit, _ = parse_mount("-90,0,0")
    preset, _ = parse_mount("side_left")
    assert np.allclose(explicit, preset)
    assert np.allclose(rpy_matrix(*MOUNT_PRESETS["side_left"]), preset)


def test_height_is_read_from_the_spec():
    matrix, floor = parse_mount("side_left@1.15")
    assert floor == pytest.approx(1.15)
    assert np.allclose(matrix, parse_mount("side_left")[0])
    assert parse_mount("upright")[1] == pytest.approx(0.05)


@pytest.mark.parametrize("spec", ["sideways", "1,2", "1,2,3,4", "side_left@-1", "side_left@high"])
def test_bad_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        parse_mount(spec)


def test_gravity_vector_follows_the_mount():
    for name in MOUNT_PRESETS:
        matrix, _ = parse_mount(name)
        kin = Kinematics(URDF, mount=matrix)
        assert np.allclose(kin.model.gravity.linear, matrix.T @ [0.0, 0.0, -GRAVITY_MS2], atol=1e-12)
        assert np.linalg.norm(kin.model.gravity.linear) == pytest.approx(GRAVITY_MS2)


def test_side_mount_gravity_points_along_base_y():
    matrix, _ = parse_mount("side_left")
    kin = Kinematics(URDF, mount=matrix)
    assert np.allclose(kin.model.gravity.linear, [0.0, GRAVITY_MS2, 0.0], atol=1e-12)


def test_inverted_mount_negates_the_torques():
    upright = Kinematics(URDF).gravity_function()
    matrix, _ = parse_mount("inverted")
    flipped = Kinematics(URDF, mount=matrix).gravity_function()
    for q in POSES:
        assert np.allclose(flipped(q), -np.asarray(upright(q)), atol=1e-9)


def test_side_mount_changes_the_torques():
    upright = Kinematics(URDF).gravity_function()
    matrix, _ = parse_mount("side_left")
    sideways = Kinematics(URDF, mount=matrix).gravity_function()
    differences = [float(np.max(np.abs(np.asarray(sideways(q)) - np.asarray(upright(q))))) for q in POSES]
    assert min(differences) > 0.5


def test_the_floor_follows_the_mount_height():
    matrix, floor = parse_mount("side_left@1.2")
    kin = Kinematics(URDF, mount=matrix, floor_m=floor)
    assert kin.floor_m == pytest.approx(1.2)
    reach = kin.world_points([0.0, 0.6, 0.0, 0.0, 0.0, 0.0])
    assert reach[:, 2].min() > -1.2
    kin.validate([0.0, 0.6, 0.0, 0.0, 0.0, 0.0])


def test_a_low_mount_blocks_the_same_pose():
    matrix, _ = parse_mount("side_left")
    kin = Kinematics(URDF, mount=matrix, floor_m=0.0)
    with pytest.raises(ValueError, match="Workspace boundary"):
        kin.validate([0.0, 0.6, 0.0, 0.0, 0.0, 0.0])


def test_controller_axes_are_expressed_in_the_base_frame(tmp_path):
    plain = make_node(tmp_path / "plain")
    tilted = make_node(tmp_path / "tilted", mount="side_left")
    assert np.allclose(plain.controllers["quest"].axes, plain.controllers["quest"].world_axes)
    quest = tilted.controllers["quest"]
    assert np.allclose(quest.axes, tilted.kin.mount.T @ quest.world_axes)
    assert not np.allclose(quest.axes, quest.world_axes)


def test_a_side_mounted_arm_tracks_world_up(tmp_path):
    tilted = make_node(tmp_path / "tilted", mount="side_left")
    quest = tilted.controllers["quest"]
    up_in_device = np.linalg.solve(quest.world_axes, [0.0, 0.0, 1.0])
    assert np.allclose(quest.axes @ up_in_device, tilted.kin.mount.T @ [0.0, 0.0, 1.0], atol=1e-12)


def test_the_mount_is_reported_in_the_status(tmp_path):
    node = make_node(tmp_path / "tilted", mount="side_left@1.15")
    status = node.status()
    assert status["mount"] == "side_left@1.15"
    assert status["floor_m"] == pytest.approx(1.15)


def test_gravity_scale_reaches_the_arm(tmp_path):
    node = TeleopNode(tmp_path / "scaled", mount="side_left@1.15", gravity_scale=0.25)
    assert node.arm.gravity_scale == pytest.approx(0.25)
    assert node.arm.status()["gravity_scale"] == pytest.approx(0.25)
