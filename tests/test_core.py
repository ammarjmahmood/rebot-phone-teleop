import math
import os
import tempfile
import time

os.environ["REBOT_DATA"] = tempfile.mkdtemp(prefix="rebot-test-")

import numpy as np
import pytest
from fastapi.testclient import TestClient
from rebot_teleop import config as C
from rebot_teleop.robot import Robot
from rebot_teleop.sequencer import Sequencer
from rebot_teleop.teleop import Teleop, XR_TO_ROBOT


def wait_until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def robot():
    r = Robot(sim=True)
    r.start()
    assert wait_until(lambda: all(u.arm.connected for u in r.units.values()))
    yield r
    r.close()


def test_power_on_holds_in_place(robot):
    before = {n: u.arm.q.copy() for n, u in robot.units.items()}
    robot.power_on()
    time.sleep(0.3)
    for name, unit in robot.units.items():
        assert unit.arm.torque
        assert np.max(np.abs(unit.arm.q - before[name])) < math.radians(0.5)


def test_one_arm_works_alone(robot):
    robot.units["right"].arm.close()
    robot.factory.buses["can2"].dead = set(range(1, 8))
    result = robot.power_on()
    assert result["powered"] == ["left"]
    assert robot.ensure_powered() == ["left"]


def test_move_tip_is_smooth_and_reaches(robot):
    robot.power_on()
    unit = robot.units["left"]
    robot.go("ready", "left")
    robot.wait(20)
    start, rotation = unit.kin.pose(unit.arm.command[:6])
    goal = start + np.array([0.0, 0.0, 0.05])
    _, err, _ = unit.move_tip(goal, rotation)
    assert err < 0.003
    samples = []
    while unit.arm.busy:
        samples.append(unit.arm.command[:6].copy())
        time.sleep(0.01)
    steps = np.abs(np.diff(np.array(samples), axis=0)).max()
    assert steps < math.radians(3.0)
    assert np.linalg.norm(unit.kin.pose(unit.arm.command[:6])[0] - goal) < 0.004


def test_out_of_reach_goes_as_close_as_possible(robot):
    unit = robot.units["left"]
    q, err, _ = unit.kin.ik(np.array([0.3, -0.9, 0.8]), np.eye(3), unit.arm.q, iterations=80)
    assert err > 0.05
    assert np.all(q >= unit.kin.lower - 1e-9) and np.all(q <= unit.kin.upper + 1e-9)


def test_quest_grip_moves_arm_relative_to_hand(robot):
    robot.power_on()
    robot.go("ready")
    robot.wait(20)
    teleop = Teleop(robot)
    unit = robot.units["right"]
    tip0 = unit.kin.pose(unit.arm.command[:6])[0]
    hand = {"p": [0.0, 1.0, -0.3], "q": [0, 0, 0, 1], "grip": 1.0, "trigger": 0.0}
    for _ in range(10):
        teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
        time.sleep(0.01)
    for _ in range(60):
        hand["p"] = [0.0, 1.05, -0.3]
        teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
        time.sleep(0.01)
    assert wait_until(lambda: np.linalg.norm(unit.kin.pose(unit.arm.command[:6])[0] - (tip0 + XR_TO_ROBOT @ np.array([0, 0.05, 0]))) < 0.006, 3)
    hand["grip"] = 0.0
    teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
    time.sleep(0.1)
    assert not teleop.hands["right"].engaged
    teleop.stop()


def test_gaze_yaw_turns_forward(robot):
    teleop = Teleop(robot)
    frame = teleop._frame(90.0)
    forward_when_looking_left = frame @ np.array([-1.0, 0.0, 0.0])
    assert np.allclose(forward_when_looking_left, [1.0, 0.0, 0.0], atol=1e-6)


def test_sequencer_points_and_recording(robot):
    robot.power_on()
    seq = Sequencer(robot)
    seq.add("demo", seq.capture())
    robot.go("ready", "left")
    robot.wait(20)
    seq.add("demo", seq.capture("left"))
    seq.start_recording("left", 50)
    unit = robot.units["left"]
    q = unit.arm.command[:6].copy()
    q[0] += math.radians(10)
    unit.arm.move(q, 60)
    robot.wait(10)
    step = seq.stop_recording("demo")
    assert len(step["t"]) > 10
    data = seq.get("demo")
    assert [s["type"] for s in data["steps"]] == ["pose", "pose", "track"]
    data["steps"].insert(0, data["steps"].pop())
    seq.save(data)
    seq.play("demo", speed=2.0)
    assert wait_until(lambda: seq.playing is None, 30)
    assert seq.progress is None


def test_server_endpoints():
    app = __import__("rebot_teleop.server", fromlist=["create_app"]).create_app(sim=True)
    with TestClient(app) as client:
        robot = app.state.robot
        assert wait_until(lambda: all(u.arm.connected for u in robot.units.values()))
        assert client.post("/api/power", json={"on": True}).json()["powered"] == ["left", "right"]
        moved = client.post("/api/move", json={"arm": "left", "pose": "ready"}).json()
        assert moved["duration_s"] > 0
        result = client.post("/api/move", json={"arm": "left", "tip_delta": [0, 0, 0.03]}).json()
        assert result["reach_error_m"] < 0.003
        status = client.get("/api/status").json()
        assert set(status["arms"]) == {"left", "right"}
        with client.websocket_connect("/ws/state") as ws:
            message = ws.receive_json()
            assert "twin" in message and "base_link" in message["twin"]["left"]
        with client.websocket_connect("/ws/teleop") as ws:
            ws.send_json({"type": "hands", "yaw": 0, "hands": {"left": {"p": [0, 1, 0], "q": [0, 0, 0, 1], "grip": 1, "trigger": 0}}})
            assert ws.receive_json()["type"] == "status"
        client.put("/api/seq/test", json={"name": "test", "steps": []})
        captured = client.post("/api/seq/test/capture", json={}).json()
        assert len(captured["steps"]) == 1
        assert client.post("/api/stop").json()["stopped"]


def test_overheat_goes_home_and_recovers(robot):
    robot.power_on()
    unit = robot.units["left"]
    robot.go("ready", "left")
    robot.wait(20)
    arm = unit.arm
    bus = robot.factory.buses["can1"]
    bus.temperature = 97.0
    assert wait_until(lambda: arm.cooling, 3)
    assert "Over temperature" in arm.fault and not unit.ready
    assert wait_until(lambda: not arm.busy, 20)
    assert np.max(np.abs(arm.command[:6] - arm.home_q)) < math.radians(0.5)
    with pytest.raises(ValueError):
        arm.servo(arm.command[:6])
    bus.temperature = 80.0
    assert wait_until(lambda: not arm.cooling, 3)
    assert arm.fault is None and unit.ready


def test_offline_arm_twin_shows_home_pose():
    r = Robot(sim=True)
    unit = r.units["left"]
    links = unit.kin.links(np.radians(C.POSES_DEG["home"]["left"]), C.GRIPPER_WIDTH_M / 2)
    assert np.allclose(np.array(r.twin()["left"]["link3"]), np.round(links["link3"][:3, :].reshape(-1), 5))


def test_reaches_below_its_base_across_the_body(robot):
    kin = robot.units["left"].kin
    rotation = kin.pose(np.radians(C.POSES_DEG["ready"]["left"]))[1]
    goal = np.array([0.35, -0.2, 0.6])
    best = min(kin.ik(goal, rotation, np.radians(seed), iterations=200)[1] for seed in ([-60, 200, 60, 0, 0, 0], [-30, 190, 80, 0, 0, 0], C.POSES_DEG["ready"]["left"]))
    assert best < 0.01


def test_failed_connect_closes_every_motor():
    from rebot_teleop.arm import Arm

    class Motor:
        closed = False

        def robstride_ping_host_id(self, *args):
            raise TimeoutError

        def close(self):
            self.closed = True

    class Controller:
        def __init__(self):
            self.motors = []
            self.closed = False

        def add_robstride_motor(self, *args):
            self.motors.append(Motor())
            return self.motors[-1]

        def close(self):
            self.closed = True

    made = []

    def factory(channel):
        made.append(Controller())
        return made[-1]

    arm = Arm("left", "can1", controller_factory=factory)
    assert not arm.connect()
    assert made[0].closed
    assert len(made[0].motors) == 7 and all(m.closed for m in made[0].motors)


def test_arm_runs_without_its_gripper_and_picks_it_up_again(robot):
    unit = robot.units["left"]
    bus = robot.factory.buses[unit.arm.channel]
    bus.dead = {7}
    unit.arm.close()
    assert wait_until(lambda: unit.arm.connected and not unit.arm.has_gripper)
    status = robot.status()["arms"]["left"]
    assert status["motors_answering"] == [1, 2, 3, 4, 5, 6]
    assert "gripper" in status["alert"]
    assert robot.power_on("left")["powered"] == ["left"]
    assert unit.arm.torque
    before = unit.arm.q.copy()
    goal = before.copy()
    goal[3] += math.radians(5)
    duration = unit.arm.move(goal, 45.0)
    assert wait_until(lambda: abs(unit.arm.q[3] - goal[3]) < math.radians(1), timeout=duration + 3)
    unit.arm.grip(0.0)
    bus.dead = set()
    robot.power_off("left")
    assert robot.power_on("left")["powered"] == ["left"]
    assert unit.arm.has_gripper
    assert robot.status()["arms"]["left"]["alert"] is None


def test_motor_fault_stops_the_arm_and_power_on_resets_it(robot):
    unit = robot.units["left"]
    robot.power_on("left")
    bus = robot.factory.buses[unit.arm.channel]
    bus.codes[1] = 16
    assert wait_until(lambda: unit.arm.fault is not None)
    assert "Joint 1 motor cut out: stall overload" in unit.arm.fault
    assert not unit.ready and unit.arm.torque
    robot.power_on("left")
    assert wait_until(lambda: unit.arm.fault is None and unit.ready)
    assert 1 not in bus.codes
    assert np.allclose(unit.arm.command[:6], unit.arm.q, atol=math.radians(1))


def test_overloaded_joint_raises_an_alert(robot):
    unit = robot.units["left"]
    robot.power_on("left")
    unit.arm.load2[0] = 16.0 ** 2
    status = robot.status()["arms"]["left"]
    assert "overloaded" in status["alert_tags"] and "Joint 1 is holding 16 Nm" in status["alert"]


def test_recorder_saves_states_and_camera_frames(robot, tmp_path):
    import json
    from types import SimpleNamespace
    from rebot_teleop.recorder import Recorder

    class Camera:
        count = 0

        def jpeg_bytes(self):
            Camera.count += 1
            return b"\xff\xd8frame", Camera.count

    recorder = Recorder(robot, SimpleNamespace(cameras={"head": Camera()}), SimpleNamespace(active=True), root=tmp_path)
    started = recorder.start()
    assert started["recording"]
    assert wait_until(lambda: recorder.samples >= 5 and recorder.frames["head"] >= 3)
    done = recorder.stop()
    assert not done["recording"] and done["error"] is None
    folder = tmp_path / done["name"]
    rows = [json.loads(line) for line in (folder / "states.jsonl").read_text().splitlines()]
    assert len(rows) >= 5 and "joints_deg" in rows[0]["arms"]["left"]
    assert (folder / "head.mjpg").read_bytes().startswith(b"\xff\xd8")
    assert len((folder / "head.times.txt").read_text().splitlines()) == done["frames"]["head"]


def test_blocked_joint_eases_off_and_frees_itself(robot):
    from rebot_teleop.arm import HOLD_LEAD, PUSH_LEAD
    unit = robot.units["left"]
    robot.power_on("left")
    bus = robot.factory.buses[unit.arm.channel]
    bus.held = {0}
    goal = unit.arm.q.copy()
    goal[0] += math.radians(40)
    unit.arm.servo(goal)
    assert wait_until(lambda: abs(unit.arm.command[0] - unit.arm.q[0]) > HOLD_LEAD[0] * 1.5, timeout=2)
    assert abs(unit.arm.command[0] - unit.arm.q[0]) <= PUSH_LEAD[0] + 1e-6
    assert wait_until(lambda: bool(unit.arm.blocked[0]), timeout=3)
    assert wait_until(lambda: abs(unit.arm.command[0] - unit.arm.q[0]) <= HOLD_LEAD[0] + 1e-6)
    status = robot.status()["arms"]["left"]
    assert "blocked" in status["alert_tags"] and "Joint 1 is pressing" in status["alert"]
    bus.held = set()
    assert wait_until(lambda: abs(unit.arm.q[0] - goal[0]) < math.radians(2), timeout=6)
    assert not unit.arm.blocked.any()


def test_torque_history_is_served():
    from rebot_teleop.server import create_app
    with TestClient(create_app(sim=True)) as client:
        data = client.get("/api/torque").json()
    assert set(data["arms"]) == {"left", "right"}
    assert data["arms"]["left"]["peak"][:4] == [36.0, 36.0, 36.0, 14.0] and data["seconds"] == 60.0
