import math
import time
from pathlib import Path
import numpy as np
import pytest

pytest.importorskip("pybullet")
from rebot_teleop import sim as simulation
from rebot_teleop.kinematics import Kinematics, mount_placement
from rebot_teleop.node import MOUNTS
from rebot_teleop.rs_arm import RSArm

try:
    simulation.find_meshes()
except FileNotFoundError:
    pytest.skip("reBot meshes not installed", allow_module_level=True)

URDF = Path(__file__).resolve().parents[1] / "assets" / "rebot_rs.urdf"
POSE = np.radians([0.0, 70.0, 60.0, 20.0, 0.0, 0.0])


def sag_with(gravity_kin):
    mount = mount_placement(**MOUNTS["left"])
    world = simulation.SimWorld()
    world.add_arm("sim-left", mount)
    arm = RSArm("sim-left", controller_factory=world.controller, gravity=gravity_kin.gravity_function(), speed_deg_s=60.0)
    try:
        arm.connect()
        arm.confirm_zero()
        arm.enable()
        arm.enabled = True
        arm.write(POSE, 0.02)
        end = time.monotonic() + 6
        while time.monotonic() < end and np.max(np.abs(arm.command[:6] - POSE)) > 1e-4:
            time.sleep(0.05)
        time.sleep(1.5)
        assert arm.fault is None
        return float(np.degrees(np.max(np.abs(arm.q - POSE))))
    finally:
        arm.close()
        world.close()


def test_mount_aware_gravity_holds_side_mounted_arm():
    mounted = Kinematics(URDF, mount_placement(**MOUNTS["left"]))
    upright = Kinematics(URDF)
    good = sag_with(mounted)
    wrong = sag_with(upright)
    assert good < 0.5
    assert wrong > 3 * max(good, 0.2)


def test_mount_changes_pose_and_gravity_consistently():
    mount = mount_placement(**MOUNTS["right"])
    mounted = Kinematics(URDF, mount)
    upright = Kinematics(URDF)
    q = np.radians([15.0, 50.0, 70.0, -10.0, 25.0, 40.0])
    p0, r0 = upright.pose(q)
    p1, r1 = mounted.pose(q)
    assert np.allclose(mount.act(p0), p1) and np.allclose(mount.rotation @ r0, r1)
    assert not np.allclose(upright.gravity_function()(q), mounted.gravity_function()(q), atol=0.3)
