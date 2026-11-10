import numpy as np
import pytest
from rebot_teleop.camera import lens, locate, optical_pose
from rebot_teleop.dojo import seeds_from


def looking_down(height):
    rotation = np.column_stack([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    return optical_pose(np.array([0.3, 0.2, height]), rotation, offset=0.0)


def test_centre_pixel_hits_the_point_below_a_downward_camera():
    pose = looking_down(1.0)
    assert np.allclose(pose[:3, 2], [0.0, 0.0, -1.0])
    capture = {"info": lens(320, 240, 80.0), "world_from_camera": pose, "depth_m": None}
    point = locate(capture, [(159.5, 119.5)], plane_z=0.68)[0]
    assert np.allclose(point["xyz"], [0.3, 0.2, 0.68], atol=1e-4)
    assert point["method"] == "plane"


def test_depth_and_plane_agree_off_centre():
    pose = looking_down(1.0)
    info = lens(320, 240, 80.0)
    plane = locate({"info": info, "world_from_camera": pose, "depth_m": None}, [(250, 60)], plane_z=0.68)[0]
    depth = np.full((240, 320), 0.32)
    measured = locate({"info": info, "world_from_camera": pose, "depth_m": depth}, [(250, 60)])[0]
    assert np.allclose(plane["xyz"], measured["xyz"], atol=1e-3)


def test_bad_pixels_and_missing_plane_are_refused():
    capture = {"info": lens(320, 240, 80.0), "world_from_camera": looking_down(1.0), "depth_m": None}
    with pytest.raises(ValueError, match="outside"):
        locate(capture, [(400, 10)], plane_z=0.68)
    with pytest.raises(ValueError, match="plane_z"):
        locate(capture, [(100, 100)])


def test_seed_ranges():
    assert seeds_from("100-103,7") == [100, 101, 102, 103, 7]
