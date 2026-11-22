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


def test_playback_is_not_cancelled_by_a_live_quest_session(robot):
    robot.power_on()
    robot.wait(20)
    seq = Sequencer(robot)
    teleop = Teleop(robot)
    teleop.yield_to = lambda: seq.playing is not None
    unit = robot.units["right"]
    start = unit.arm.command[:6].copy()
    goal = start.copy()
    goal[1] += math.radians(30)
    seq.save({"name": "lift", "steps": [{"type": "pose", "arms": {"right": np.degrees(goal).tolist()}, "speed_deg_s": 40}]})
    hand = {"p": [0.0, 1.0, -0.3], "q": [0, 0, 0, 1], "grip": 1.0, "trigger": 0.0}
    teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
    seq.play("lift")
    for _ in range(80):
        teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
        teleop._step(time.monotonic(), 0.01)
        time.sleep(0.01)
    assert wait_until(lambda: seq.playing is None, 20)
    assert np.allclose(unit.arm.command[:6], goal, atol=math.radians(1))
    teleop.stop()


def test_track_playback_smooths_held_readings():
    from rebot_teleop.sequencer import condition, TRACK_DT, TRACK_EASE_S, TRACK_SMOOTH_S
    t = np.arange(0.0, 3.0, 0.02)
    held = np.floor(t * 10.0) / 10.0
    rows = np.stack([held * 60.0] + [np.zeros_like(t)] * 6, axis=1)
    grid, out = condition(t.tolist(), rows.tolist())
    speed = np.diff(out[:, 0]) / TRACK_DT
    ease = max(TRACK_EASE_S, 3 * TRACK_SMOOTH_S)
    middle = speed[int((ease + 3 * TRACK_SMOOTH_S) / TRACK_DT):int((ease + t[-1] - 3 * TRACK_SMOOTH_S) / TRACK_DT)]
    assert len(middle) > 50
    assert np.all(np.abs(middle - 60.0) < 3.0)


def test_stronger_smoothing_cuts_jerk_and_keeps_the_end_points():
    from rebot_teleop.sequencer import condition, TRACK_DT
    rng = np.random.default_rng(5)
    t = np.arange(0.0, 4.0, 0.02)
    clean = 40 * np.sin(1.2 * t)
    shaky = clean + np.cumsum(rng.normal(0, 0.4, len(t)))
    rows = np.stack([shaky] + [np.zeros_like(t)] * 5 + [np.full_like(t, 0.05)], axis=1)
    jerks = []
    for smooth in (0.1, 0.2, 0.4):
        grid, out = condition(t, rows, smooth=smooth)
        jerk = np.diff(out[:, 0], 3) / TRACK_DT ** 3
        jerks.append(np.sqrt(np.mean(jerk ** 2)))
        assert out[0, 0] == pytest.approx(shaky[0], abs=0.05) and out[-1, 0] == pytest.approx(shaky[-1], abs=0.05)
    assert jerks[0] > jerks[1] > jerks[2]


def test_two_recordings_play_at_the_same_time_one_per_arm(robot):
    robot.power_on()
    robot.wait(20)
    seq = Sequencer(robot)
    left, right = robot.units["left"].arm, robot.units["right"].arm
    lgoal = left.command[:6].copy()
    lgoal[0] += math.radians(25)
    rgoal = right.command[:6].copy()
    rgoal[0] -= math.radians(25)
    seq.save({"name": "wave left", "steps": [{"type": "pose", "arms": {"left": np.degrees(lgoal).tolist()}, "speed_deg_s": 20}]})
    seq.save({"name": "wave right", "steps": [{"type": "wait", "seconds": 0.2}, {"type": "pose", "arms": {"right": np.degrees(rgoal).tolist(), "left": np.degrees(left.command[:6]).tolist()}, "speed_deg_s": 20}]})
    seq.play_together({"left": "wave left", "right": "wave right"})
    both = False
    end = time.monotonic() + 20
    while seq.playing is not None and time.monotonic() < end:
        both = both or (left.busy and right.busy)
        time.sleep(0.01)
    assert seq.playing is None and seq.progress is None
    assert both
    assert np.allclose(left.command[:6], lgoal, atol=math.radians(1))
    assert np.allclose(right.command[:6], rgoal, atol=math.radians(1))
    with pytest.raises(ValueError):
        seq.play_together({"left": "wave right", "right": "wave left"})
    with pytest.raises(ValueError):
        seq.play_together({"left": "wave left", "right": None})


def test_play_together_can_pick_one_motion_inside_a_recording(robot):
    robot.power_on()
    robot.wait(20)
    seq = Sequencer(robot)
    left, right = robot.units["left"].arm, robot.units["right"].arm
    l0, r0 = np.degrees(left.command[:6]).tolist(), np.degrees(right.command[:6]).tolist()
    t = [i * 0.02 for i in range(60)]
    def track(arm, joint, deg):
        rows = {"left": [l0 + [0.05]] * 60, "right": [r0 + [0.05]] * 60}
        moving = []
        for i in range(60):
            q = list(l0 if arm == "left" else r0)
            q[joint] += deg * i / 59
            moving.append(q + [0.05])
        rows[arm] = moving
        return {"type": "track", "label": f"{arm} motion", "rate_hz": 50.0, "t": t, "arms": rows, "speed": 1.0, "pause_s": 0.0}
    seq.save({"name": "mixed", "steps": [track("right", 0, -20), track("left", 0, 20)]})
    motions = [s for s in seq.list() if s["name"] == "mixed"][0]["motions"]
    assert [m["moves"] for m in motions] == [["right"], ["left"]]
    seq.play_together({"left": {"name": "mixed", "step": 1}, "right": {"name": "mixed", "step": 0}})
    assert wait_until(lambda: seq.playing is None, 20)
    assert seq.progress is None
    assert math.degrees(left.command[0]) == pytest.approx(l0[0] + 20, abs=1)
    assert math.degrees(right.command[0]) == pytest.approx(r0[0] - 20, abs=1)


def test_arms_power_off_after_play_both_only_when_it_finishes(robot):
    robot.power_on()
    robot.wait(20)
    seq = Sequencer(robot)
    left, right = robot.units["left"].arm, robot.units["right"].arm
    lgoal, rgoal = left.command[:6].copy(), right.command[:6].copy()
    lgoal[0] += math.radians(10)
    rgoal[0] -= math.radians(10)
    seq.save({"name": "l", "steps": [{"type": "pose", "arms": {"left": np.degrees(lgoal).tolist()}, "speed_deg_s": 40}]})
    seq.save({"name": "r", "steps": [{"type": "pose", "arms": {"right": np.degrees(rgoal).tolist()}, "speed_deg_s": 40}]})
    seq.play_together({"left": "l", "right": "r"}, power_off=True)
    assert wait_until(lambda: seq.playing is None, 20)
    assert not left.torque and not right.torque
    robot.power_on()
    seq.save({"name": "slow", "steps": [{"type": "pose", "arms": {"left": np.degrees(left.command[:6] + np.radians([30, 0, 0, 0, 0, 0])).tolist()}, "speed_deg_s": 10}]})
    seq.save({"name": "slow2", "steps": [{"type": "pose", "arms": {"right": np.degrees(right.command[:6] - np.radians([30, 0, 0, 0, 0, 0])).tolist()}, "speed_deg_s": 10}]})
    seq.play_together({"left": "slow", "right": "slow2"}, power_off=True)
    time.sleep(0.5)
    seq.stop()
    assert wait_until(lambda: seq.playing is None, 10)
    assert left.torque and right.torque
    seq.save({"name": "both", "steps": [{"type": "wait", "seconds": 0.2}]})
    seq.play("both")
    assert wait_until(lambda: seq.playing is None, 10)
    assert left.torque and right.torque


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
            client.post("/api/power", json={"on": False, "arm": "left"})
            assert not robot.units["left"].ready
            ws.send_json({"type": "enable", "arm": "left"})
            reply = ws.receive_json()
            assert reply["enabled"] == {"asked": ["left"], "powered": ["left"]} and reply["arms"]["left"]["ready"]
            ws.send_json({"type": "hands", "yaw": 0, "hands": {"left": {"p": [0, 1, 0], "q": [0, 0, 0, 1], "grip": 1, "trigger": 0}}})
            assert wait_until(lambda: app.state.teleop.hands["left"].engaged, 2)
            ws.send_json({"type": "home"})
            while "home" not in (reply := ws.receive_json()):
                pass
            assert "grips" in reply["home"]["error"]
            assert wait_until(lambda: not app.state.teleop.hands["left"].engaged, 2)
            ws.send_json({"type": "home"})
            while "home" not in (reply := ws.receive_json()):
                pass
            assert reply["home"]["moving"] == ["left", "right"] and reply["home"]["duration_s"] > 0
            robot.wait(30)
            for name, unit in robot.units.items():
                assert np.allclose(np.degrees(unit.arm.q), C.POSES_DEG["home"][name], atol=1.0)
        client.put("/api/seq/test", json={"name": "test", "steps": []})
        captured = client.post("/api/seq/test/capture", json={}).json()
        assert len(captured["steps"]) == 1
        assert client.post("/api/stop").json()["stopped"]


def test_overheat_pulls_in_and_recovers(robot):
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


def test_arm_runs_without_wrist_twist_only_when_allowed(robot):
    unit = robot.units["left"]
    bus = robot.factory.buses[unit.arm.channel]
    held_at = unit.arm.q[5]
    unit.arm.joint_memory = unit.arm.q.copy()
    bus.dead = {6}
    unit.arm.close()
    assert wait_until(lambda: "6" in (robot.status()["arms"]["left"]["note"] or ""), timeout=5)
    assert not unit.arm.connected
    unit.arm.without = {5}
    assert wait_until(lambda: unit.arm.connected, timeout=5)
    assert unit.arm.held == [5] and unit.arm.has_gripper
    assert "joint 6" in robot.status()["arms"]["left"]["note"]
    assert robot.power_on("left")["powered"] == ["left"]
    goal = unit.arm.q.copy()
    goal[0] += math.radians(10)
    goal[5] += math.radians(40)
    duration = unit.arm.move(goal, 45.0)
    assert wait_until(lambda: abs(unit.arm.q[0] - goal[0]) < math.radians(1), timeout=duration + 3)
    time.sleep(0.2)
    assert unit.arm.q[5] == pytest.approx(held_at, abs=1e-6)
    assert unit.arm.command[5] == pytest.approx(held_at, abs=1e-6)
    unit.arm.grip(0.0)
    bus.dead = set()
    unit.arm.without = set()
    robot.power_off("left")
    unit.arm.close()
    assert wait_until(lambda: unit.arm.connected, timeout=5)
    assert unit.arm.held == []
    assert robot.status()["arms"]["left"]["note"] is None


def test_motion_recorded_without_a_gripper_leaves_the_gripper_alone(robot):
    unit = robot.units["left"]
    bus = robot.factory.buses[unit.arm.channel]
    bus.dead = {7}
    unit.arm.close()
    assert wait_until(lambda: unit.arm.connected and not unit.arm.has_gripper)
    seq = Sequencer(robot)
    seq.start_recording("left", 50)
    time.sleep(0.3)
    step = seq.stop_recording("nogrip")
    assert step["no_gripper"] == ["left"]
    assert "left" not in seq.capture("left")["grippers"]
    bus.dead = set()
    robot.power_off("left")
    assert robot.power_on("left")["powered"] == ["left"]
    assert unit.arm.has_gripper
    unit.arm.grip(C.GRIPPER_WIDTH_M)
    assert wait_until(lambda: unit.arm.width > 0.04, timeout=5)
    seq.play("nogrip")
    assert wait_until(lambda: seq.playing is None, 20)
    assert seq.progress is None
    assert unit.arm.width_goal > 0.04


def test_left_arm_records_and_plays_joints_and_gripper_without_joint_6(robot):
    unit = robot.units["left"]
    arm = unit.arm
    bus = robot.factory.buses[arm.channel]
    arm.joint_memory = arm.q.copy()
    bus.dead = {6}
    arm.without = {5}
    arm.close()
    assert wait_until(lambda: arm.connected and arm.held == [5] and arm.has_gripper, timeout=5)
    robot.power_off("left")
    seq = Sequencer(robot)
    seq.start_recording("left", 50)
    start = bus.pos.copy()
    for i in range(1, 41):
        with bus.lock:
            bus.pos[0] = start[0] + math.radians(15) * i / 40
            bus.pos[6] = arm.gripper_open + (arm.gripper_closed - arm.gripper_open) * i / 40
            bus.goal[:] = bus.pos
        time.sleep(0.025)
    time.sleep(0.3)
    step = seq.stop_recording("bytouch")
    assert "no_gripper" not in step
    widths = np.array(step["arms"]["left"])[:, 6]
    assert widths[0] > 0.04 and widths[-1] < 0.01
    with bus.lock:
        bus.pos[0] = start[0]
        bus.pos[6] = arm.gripper_open
        bus.goal[:] = bus.pos
    time.sleep(0.3)
    assert robot.power_on("left")["powered"] == ["left"]
    seq.play("bytouch")
    assert wait_until(lambda: seq.playing is None, 20)
    assert seq.progress is None
    assert math.degrees(arm.command[0] - start[0]) == pytest.approx(15, abs=1.5)
    assert arm.width_goal < 0.01
    assert arm.held == [5]


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


def test_reading_that_jumps_a_whole_turn_keeps_the_joint_where_it_is(robot):
    unit = robot.units["left"]
    robot.power_on("left")
    time.sleep(0.2)
    bus = robot.factory.buses[unit.arm.channel]
    before = unit.arm.q.copy()
    with bus.lock:
        bus.pos[1] -= 2 * math.pi
        bus.goal[1] -= 2 * math.pi
        shifted = bus.pos[1]
    time.sleep(0.4)
    assert unit.arm.turn_fixes == 1
    assert np.max(np.abs(unit.arm.q - before)) < math.radians(1)
    assert abs(bus.pos[1] - shifted) < math.radians(1)
    robot.power_off("left")
    with bus.lock:
        bus.pos[2] += 2 * math.pi
        bus.goal[2] += 2 * math.pi
    unit.arm.refresh()
    assert unit.arm.turn_fixes == 2
    assert np.max(np.abs(unit.arm.q - before)) < math.radians(1)


def test_gripper_pushed_past_open_by_hand_still_reads_open_after_power_on(robot):
    unit = robot.units["right"]
    bus = robot.factory.buses[unit.arm.channel]
    robot.power_off("right")
    with bus.lock:
        bus.pos[6] = math.radians(356.0)
        bus.goal[6] = bus.pos[6]
    unit.arm.refresh()
    assert unit.arm.width == pytest.approx(C.GRIPPER_WIDTH_M)
    assert robot.power_on("right")["powered"] == ["right"]
    assert math.degrees(unit.arm.measured[6]) == pytest.approx(356.0, abs=1)
    assert unit.arm.width == pytest.approx(C.GRIPPER_WIDTH_M)
    robot.power_off("right")
    assert wait_until(lambda: unit.arm.gripper_memory is not None and math.degrees(unit.arm.gripper_memory) == pytest.approx(356.0, abs=2))
    unit.arm.position_known = False
    assert robot.power_on("right")["powered"] == ["right"]
    assert math.degrees(unit.arm.measured[6]) == pytest.approx(356.0, abs=1)


def test_closed_gripper_reads_closed_when_its_remembered_angle_is_stale(robot):
    unit = robot.units["left"]
    unit.arm.gripper_memory = math.radians(270.8)
    raw = np.radians([0, 0, 0, 0, 0, 0, -5.5])
    assert math.degrees(unit.arm._wrap_offsets(raw)[6]) == pytest.approx(0.0)
    unit.arm.gripper_memory = math.radians(352.0)
    assert math.degrees(unit.arm._wrap_offsets(raw)[6]) == pytest.approx(360.0)
    unit.arm.gripper_memory = None
    assert math.degrees(unit.arm._wrap_offsets(raw)[6]) == pytest.approx(0.0)


def test_wider_gripper_range_still_reads_closed_and_fully_open(robot):
    arm = robot.units["left"].arm
    arm.set_gripper_range(0.0, math.radians(338.0))
    arm.gripper_memory = None
    for deg in (-5.5, 0.0, 120.0, 338.0, 341.0):
        raw = np.radians([0, 0, 0, 0, 0, 0, deg])
        for shift in (0.0, 360.0, -360.0):
            raw[6] = math.radians(deg + shift)
            assert math.degrees(raw[6] + arm._wrap_offsets(raw)[6]) == pytest.approx(deg)
    assert arm.width_to_angle(C.GRIPPER_WIDTH_M) == pytest.approx(math.radians(338.0))


def test_wrist_roll_turn_is_remembered_across_a_server_restart(robot):
    from rebot_teleop.robot import Unit
    unit = robot.units["left"]
    robot.power_off("left")
    saved = lambda: C.load("arms/left/joints.json", {}).get("deg")
    assert wait_until(lambda: saved() is not None and np.allclose(saved(), np.degrees(unit.arm.measured[:6]), atol=2))
    fresh = Unit("left", robot, factory=robot.factory)
    assert np.allclose(np.degrees(fresh.arm.joint_memory), saved(), atol=0.2)
    raw = np.radians([0, 0, 0, 0, 0, 180.0, 0])
    fresh.arm.joint_memory = np.radians([0, 0, 0, 0, 0, -175.0])
    assert math.degrees(fresh.arm._wrap_offsets(raw)[5]) == pytest.approx(-360.0)
    fresh.arm.joint_memory = np.radians([0, 0, 0, 0, 0, 175.0])
    assert math.degrees(fresh.arm._wrap_offsets(raw)[5]) == pytest.approx(0.0)


def test_play_together_starts_both_motions_together(robot):
    robot.power_on()
    robot.wait(20)
    seq = Sequencer(robot)
    left, right = robot.units["left"].arm, robot.units["right"].arm
    l0, r0 = np.degrees(left.command[:6]).tolist(), np.degrees(right.command[:6]).tolist()
    far = list(l0)
    far[5] += 120
    t = [i * 0.02 for i in range(50)]
    def rows(start, joint, deg):
        out = []
        for i in range(50):
            q = list(start)
            q[joint] += deg * i / 49
            out.append(q + [0.05])
        return out
    seq.save({"name": "late", "steps": [{"type": "track", "rate_hz": 50.0, "t": t, "arms": {"left": rows(far, 0, 15)}, "speed": 1.0, "pause_s": 0.0}]})
    seq.save({"name": "early", "steps": [{"type": "track", "rate_hz": 50.0, "t": t, "arms": {"right": rows(r0, 0, -15)}, "speed": 1.0, "pause_s": 0.0}]})
    seq.play_together({"left": "late", "right": "early"})
    started = {}
    end = time.monotonic() + 30
    while seq.playing is not None and time.monotonic() < end:
        for name, arm, base in (("left", left, l0[0]), ("right", right, r0[0])):
            if name not in started and abs(math.degrees(arm.command[0]) - base) > 1.0:
                started[name] = time.monotonic()
        time.sleep(0.005)
    assert seq.progress is None
    assert set(started) == {"left", "right"}
    assert abs(started["left"] - started["right"]) < 0.3


def test_gripper_angle_is_remembered_across_a_server_restart(robot):
    from rebot_teleop.robot import Unit
    unit = robot.units["right"]
    bus = robot.factory.buses[unit.arm.channel]
    robot.power_off("right")
    with bus.lock:
        bus.pos[6] = math.radians(356.0)
        bus.goal[6] = bus.pos[6]
    assert wait_until(lambda: C.load("arms/right/gripper.json", {}).get("deg") == pytest.approx(356.0, abs=1))
    fresh = Unit("right", robot, factory=robot.factory)
    assert math.degrees(fresh.arm.gripper_memory) == pytest.approx(356.0, abs=1)
    assert math.degrees(fresh.arm._wrap_offsets(np.radians([0, 0, 0, 0, 0, 0, -4.0]))[6]) == pytest.approx(360.0)


def test_sequencer_warns_when_a_recording_goes_past_what_playback_can_do(robot, tmp_path, monkeypatch):
    import rebot_teleop.sequencer as S
    monkeypatch.setattr(S, "FOLDER", tmp_path)
    seq = Sequencer(robot)
    unit = robot.units["right"]
    bus = robot.factory.buses[unit.arm.channel]
    seq.start_recording("right", 50)
    time.sleep(0.2)
    joints = [math.degrees(v) for v in unit.arm.q]
    joints[2] = 230.0
    bus.move_by_hand(joints)
    assert wait_until(lambda: seq.status()["recording"]["over"], 5)
    step = seq.stop_recording("cut")
    assert {(o["arm"], o["joint"], o["limit"]) for o in step["over"]} == {("right", 3, 178)}
    seq.add("cut", {"type": "pose", "arms": {"left": [0, 90, 90, 0, 0, 0]}, "grippers": {}, "speed_deg_s": 90, "pause_s": 0})
    plan = seq.plan(seq.get("cut"))
    assert len(plan["warnings"]) == 1 and "right joint 3" in plan["warnings"][0]
    assert plan["steps"][0]["over"] and "over" not in plan["steps"][1]


def test_heat_budget_follows_the_motor_tables():
    from rebot_teleop import heat as H
    assert H.seconds_at(H.TABLES["rs-06"]["still"], 8.0) == math.inf
    assert H.seconds_at(H.TABLES["rs-06"]["still"], 11.0) == pytest.approx(200.0)
    assert H.seconds_at(H.TABLES["rs-06"]["still"], 17.0) == pytest.approx(15.0)
    assert 200.0 < H.seconds_at(H.TABLES["rs-06"]["still"], 9.5) < 1000.0
    assert H.seconds_at(H.TABLES["rs-06"]["moving"], 20.0) == pytest.approx(36.0)
    assert H.seconds_at(H.TABLES["rs-00"]["still"], 7.0) == pytest.approx(43.0)
    budget = H.Budget(["rs-06"] * 3 + ["rs-00"] * 3)
    for _ in range(1000):
        budget.step([11.0, 0, 0, 0, 0, 0], [0] * 6, 0.1)
    assert budget.used[0] == pytest.approx(0.5, abs=0.01) and budget.used[1] == 0.0
    assert budget.seconds_left()[0] == pytest.approx(60.0, abs=2.0)
    moving = H.Budget(["rs-06"])
    moving.step([10.0], [90.0], 1.0)
    assert moving.rates[0] == 0.0
    moving.step([4.0], [90.0], 1.0)
    assert moving.rates[0] < 0
    for _ in range(300):
        budget.step([0.0] * 6, [0] * 6, 0.1)
    assert budget.used[0] == pytest.approx(0.4, abs=0.01)
    summary = budget.summary()
    assert summary["joint"] == 1 and summary["state"] == "cooling" and summary["seconds"] is None


def test_heating_joint_raises_an_alert_with_time_left(robot):
    unit = robot.units["left"]
    robot.power_on("left")
    bus = robot.factory.buses[unit.arm.channel]
    with unit.arm.lock:
        unit.arm.heat.used[0] = 0.6
    bus.torque[0] = 11.0
    assert wait_until(lambda: "heating" in robot.status()["arms"]["left"]["alert_tags"])
    status = robot.status()["arms"]["left"]
    assert "Joint 1 has used 6" in status["alert"] and status["heat"]["joint"] == 1 and 30 < status["heat"]["seconds"] < 45


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
    assert data["arms"]["left"]["rated"] == [8.0, 8.0, 8.0, 5.0, 5.0, 5.0]


def far_right_pose(robot):
    unit = robot.units["right"]
    down = np.radians(C.POSES_DEG["ready"]["right"])
    q, _, _ = unit.kin.ik(np.array([0.50, -0.20, 0.95]), unit.kin.pose(down)[1], down, iterations=200)
    return q


def test_arm_out_of_budget_pulls_in_until_it_can_hold_then_hands_back_control(robot):
    robot.power_on()
    unit = robot.units["right"]
    arm = unit.arm
    arm.move(far_right_pose(robot), 90)
    robot.wait(20)
    time.sleep(0.5)
    start = unit.kin.pose(arm.q)[0]
    start_load = abs(arm.effort[0])
    assert start_load > 9
    with arm.lock:
        arm.heat.used[0] = arm.heat.limit - 0.001
    assert wait_until(lambda: arm.resting, 3)
    assert "pulling the hand in" in arm.fault
    assert wait_until(lambda: not arm.resting, 20)
    assert arm.fault is None and unit.ready
    tip = unit.kin.pose(arm.q)[0]
    assert tip[0] < start[0] - 0.05 and tip[2] >= start[2] - 0.01
    assert abs(arm.effort[0]) <= C.HEAT_HOLD_MARGIN * arm.continuous[0] + 0.3
    assert np.max(np.abs(arm.q - arm.home_q)) > math.radians(20)


def test_sequence_style_rest_waits_then_goes_back_and_stop_keeps_it_in(robot):
    robot.power_on()
    unit = robot.units["right"]
    arm = unit.arm
    arm.move(far_right_pose(robot), 90)
    robot.wait(20)
    start = arm.q.copy()
    with arm.lock:
        arm.heat.used[:] = 0.5
    assert arm.relax("Test rest")
    assert wait_until(lambda: arm.rest_phase == "recovering", 20)
    assert "Resting here" in arm.fault
    with arm.lock:
        arm.heat.used[:] = 0.0
    assert wait_until(lambda: not arm.resting, 20)
    assert np.max(np.abs(arm.q - start)) < math.radians(2)
    with arm.lock:
        arm.heat.used[:] = 0.5
    assert arm.relax("Test rest")
    assert wait_until(lambda: arm.rest_phase == "recovering", 20)
    robot.stop()
    with arm.lock:
        arm.heat.used[:] = 0.0
    assert wait_until(lambda: not arm.resting, 5)
    time.sleep(0.3)
    assert np.max(np.abs(arm.q - start)) > math.radians(5) and unit.ready


def test_motor_that_cuts_out_is_caught_and_relaxes_until_power_on(robot):
    robot.power_on()
    unit = robot.units["left"]
    robot.go("ready", "left")
    robot.wait(20)
    arm = unit.arm
    bus = robot.factory.buses[arm.channel]
    bus.codes[1] = 16
    assert wait_until(lambda: arm.fault is not None and "caught" in arm.fault, 3)
    assert 1 not in bus.codes and arm.torque and not unit.ready
    assert wait_until(lambda: not arm.busy, 20)
    assert "caught" in arm.fault and not unit.ready
    robot.power_on("left")
    assert wait_until(lambda: arm.fault is None and unit.ready)


def test_sequence_plan_counts_rests_and_flags_impossible_steps(robot):
    robot.power_on()
    seq = Sequencer(robot)
    ready = C.POSES_DEG["ready"]
    heavy = {"type": "pose", "arms": {"right": ready["right"]}, "speed_deg_s": 90, "pause_s": 300.0, "label": "long hold"}
    arm = robot.units["right"].arm
    with arm.lock:
        arm.heat.used[0] = 0.6
    plan = seq.plan({"name": "t", "steps": [heavy]})
    assert plan["ok"] and 20 < plan["steps"][0]["rest_s"] < 120
    with arm.lock:
        arm.heat.used[:] = 0.0
    stretched = {"type": "pose", "arms": {"right": [1.4, 175.0, 147.1, 13.9, -13.0, -158.1]}, "speed_deg_s": 90, "pause_s": 120.0, "label": "reach out"}
    plan = seq.plan({"name": "t", "steps": [stretched]})
    assert not plan["ok"] and "joint 1 of the right arm" in plan["problems"][0]


def test_sequence_rests_before_a_step_that_would_run_out(robot):
    robot.power_on()
    seq = Sequencer(robot)
    ready = C.POSES_DEG["ready"]
    arm = robot.units["right"].arm
    start = arm.q.copy()
    with arm.lock:
        arm.heat.used[0] = 0.75
    seq.save({"name": "rest test", "steps": [{"type": "pose", "arms": {"right": ready["right"]}, "speed_deg_s": 90, "pause_s": 0.5, "label": "go ready"}]})
    seq.play("rest test")
    assert wait_until(lambda: arm.resting, 5)
    assert "Resting before step 1" in arm.fault
    assert wait_until(lambda: arm.rest_phase == "recovering", 20)
    with arm.lock:
        arm.heat.used[:] = 0.0
    assert wait_until(lambda: seq.playing is None, 30)
    assert seq.progress is None
    assert np.max(np.abs(np.degrees(arm.q) - ready["right"])) < 2.0
    assert np.max(np.abs(arm.q - start)) > math.radians(10)


def test_hold_here_holds_a_hand_placed_arm_where_it_is():
    from rebot_teleop.server import create_app
    with TestClient(create_app(sim=True)) as client:
        robot = client.app.state.robot
        assert wait_until(lambda: all(u.arm.connected for u in robot.units.values()))
        unit = robot.units["left"]
        placed = [-40.0, 120.0, 60.0, 20.0, 10.0, 0.0]
        robot.factory.buses[unit.arm.channel].move_by_hand(placed)
        assert wait_until(lambda: abs(unit.arm.q[0] - math.radians(-40.0)) < math.radians(0.5))
        data = client.post("/api/hold", json={}).json()
        assert sorted(data["holding"]) == ["left", "right"]
        time.sleep(0.5)
        assert unit.arm.torque and np.max(np.abs(np.degrees(unit.arm.q) - placed)) < 0.5


def test_counterbalance_setting_lightens_joint_1_and_guards_the_drum():
    settings = {"counterbalance": {"nm_per_rad": 3.0, "zero_deg": 90.0, "forward": -1, "max_deg": 170, "j2_min_deg": -20}}
    C.save("arms/right/settings.json", settings)
    try:
        r = Robot(sim=True)
        unit = r.units["right"]
        ready = np.radians(C.POSES_DEG["ready"]["right"])
        assisted = unit.kin.gravity_function()(ready)[0]
        assert assisted == pytest.approx(-7.53 + 3.0 * math.radians(90 - 28.5), abs=0.1)
        assert abs(unit.kin.gravity_function()(np.radians(C.POSES_DEG["home"]["right"]))[0]) < 0.3
        assert unit.arm.lower[1] == pytest.approx(math.radians(-20)) and unit.kin.lower[1] == pytest.approx(math.radians(-20))
        assert r.units["left"].arm.lower[1] == pytest.approx(math.radians(C.LIMITS_DEG[1][0]))
    finally:
        (C.DATA / "arms/right/settings.json").unlink()


def test_joint_1_can_be_an_rs03():
    from rebot_teleop.arm import Arm
    from rebot_teleop import heat as H
    arm = Arm("x", "can9", motors={"1": "rs-03"})
    assert arm.models[0] == "rs-03" and arm.motor_list[0] == (1, "rs-03") and arm.motor_list[1] == (2, "rs-06")
    assert arm.continuous[0] == 15.0 and arm.peak[0] == 60.0
    assert H.seconds_at(H.TABLES["rs-03"]["still"], 20.0) == pytest.approx(30.0)
    assert H.heat_rate("rs-03", 12.0, 0) < 0


def test_hanging_weight_counterbalance_is_a_constant_pull():
    C.save("arms/right/settings.json", {"counterbalance": {"constant_nm": 4.0, "forward": -1}})
    try:
        unit = Robot(sim=True).units["right"]
        gravity = unit.kin.gravity_function()
        assert gravity(np.radians(C.POSES_DEG["ready"]["right"]))[0] == pytest.approx(-3.53, abs=0.1)
        assert gravity(np.radians(C.POSES_DEG["home"]["right"]))[0] == pytest.approx(4.1, abs=0.1)
    finally:
        (C.DATA / "arms/right/settings.json").unlink()


def test_motor_log_records_poses_events_and_reports_costly_poses(robot, tmp_path):
    from rebot_teleop.motorlog import MotorLog, report_text
    log = MotorLog(robot, root=tmp_path, hz=50.0)
    log.start()
    try:
        robot.power_on("right")
        arm = robot.units["right"].arm
        far = [1.4, 175.0, 147.1, 13.9, -13.0, -158.1]
        arm.move(np.radians(far), 90.0)
        robot.wait(20)
        time.sleep(2.5)
        arm.relax("Test rest")
        time.sleep(0.5)
    finally:
        log.stop()
    report = log.report(hours=1)
    right = report["arms"]["right"]
    assert right["powered_min"] > 0 and right["costly_poses"]
    worst = right["costly_poses"][0]
    assert abs(worst["j1_mean_nm"]) > 12 and worst["j1_j2_j3_deg"][1] == 180
    assert any("Test rest" in e["event"] or "relax" in e["event"] for e in report["events"])
    assert all(len(e["q"]) == 6 for e in report["events"])
    text = report_text(report)
    assert "poses that use joint 1's budget most" in text and "right arm" in text


def test_reach_guard_stops_teleop_reaching_out_when_the_budget_is_low(robot):
    robot.power_on()
    robot.go("ready")
    robot.wait(20)
    teleop = Teleop(robot)
    unit = robot.units["right"]
    arm = unit.arm

    def reach(used):
        robot.go("ready", "right")
        robot.wait(20)
        with arm.lock:
            arm.heat.used[:] = used
        hand = {"p": [0.0, 1.0, -0.3], "q": [0, 0, 0, 1], "grip": 0.0, "trigger": 0.0}
        teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
        time.sleep(0.1)
        hand["grip"] = 1.0
        for _ in range(10):
            teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
            time.sleep(0.01)
        for _ in range(80):
            hand["p"] = [0.0, 1.05, -0.65]
            with arm.lock:
                arm.heat.used[:] = used
            teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
            time.sleep(0.01)
        time.sleep(0.5)
        return abs(unit.load_at(arm.target[:6])[0]), teleop.status()["hands"]["right"]["reach_limited"]

    free_load, free_limited = reach(0.0)
    guarded_load, guarded_limited = reach(0.6)
    teleop.stop()
    assert free_load > 9.0 and not free_limited
    assert guarded_load <= 8.3 and guarded_limited


def test_autonomous_move_reports_the_load_it_will_hold():
    from rebot_teleop.server import create_app
    with TestClient(create_app(sim=True)) as client:
        robot = client.app.state.robot
        assert wait_until(lambda: all(u.arm.connected for u in robot.units.values()))
        robot.power_on("right")
        data = client.post("/api/move", json={"arm": "right", "joints_deg": [1.4, 175.0, 147.1, 13.9, -13.0, -158.1], "speed_deg_s": 90}).json()
        assert data["load"]["worst"]["joint"] == 1 and abs(data["load"]["worst"]["nm"]) > 15
        assert "Joint 1 holds about" in data["load_note"]
        easy = client.post("/api/move", json={"pose": "home", "arm": "right"}).json()
        assert "load_note" not in easy


def test_table_guard_refuses_moves_into_the_table_once_it_is_measured(robot):
    robot.power_on("right")
    robot.go("ready", "right")
    robot.wait(20)
    unit = robot.units["right"]
    down = np.radians(C.POSES_DEG["ready"]["right"])
    low, _, _ = unit.kin.ik(np.array([0.32, -0.25, 0.60]), unit.kin.pose(down)[1], down, iterations=150)
    assert unit.table_hit(low) is None
    robot.set_table(top=0.68, front=0.20)
    try:
        assert "into the table" in unit.table_hit(low)
        with pytest.raises(ValueError):
            unit.move_tip([0.32, -0.25, 0.60])
        high, _, _ = unit.kin.ik(np.array([0.32, -0.25, 0.80]), unit.kin.pose(down)[1], down, iterations=150)
        assert unit.table_hit(high) is None
    finally:
        robot.set_table(front=-1)


def test_heavy_poses_switch_lifts_the_guard_and_sequence_rests():
    from rebot_teleop.server import create_app
    with TestClient(create_app(sim=True)) as client:
        robot = client.app.state.robot
        assert wait_until(lambda: all(u.arm.connected for u in robot.units.values()))
        assert client.post("/api/limits", json={"heavy": True}).json()["relax_at_pct"] == 95
        assert robot.heavy and robot.units["right"].arm.heat.limit == C.HEAT_LOWER_HEAVY
        assert client.get("/api/status").json()["heavy"] is True
        seq = client.app.state.sequencer
        far = {"type": "pose", "arms": {"right": [1.4, 175.0, 147.1, 13.9, -13.0, -158.1]}, "speed_deg_s": 90, "pause_s": 120.0, "label": "reach out"}
        plan = seq.plan({"name": "t", "steps": [far]})
        assert plan["ok"] and plan["heavy"] and plan["problems"]
        assert seq._prepare(0, far, 1.0) is True
        client.post("/api/limits", json={"heavy": False})
        assert not robot.heavy and robot.units["right"].arm.heat.limit == C.HEAT_LOWER
        assert not seq.plan({"name": "t", "steps": [far]})["ok"]


def test_supply_voltage_is_logged_with_the_lowest_dip_between_rows(robot, tmp_path):
    from rebot_teleop.motorlog import MotorLog, report_text
    log = MotorLog(robot, root=tmp_path, hz=5.0, voltage_hz=100.0)
    log.start()
    try:
        robot.power_on("right")
        arm = robot.units["right"].arm
        bus = robot.factory.buses[arm.channel]
        assert wait_until(lambda: all(v is not None for v in arm.voltage))
        assert arm.status()["supply_v"] == [48.0, 48.0]
        time.sleep(0.5)
        bus.voltage = 31.5
        time.sleep(0.08)
        bus.voltage = 48.0
        time.sleep(0.6)
        bus.voltage = 2.0
        assert wait_until(lambda: arm.voltage == [None, None])
        assert arm.torque and not arm.fault
    finally:
        log.stop()
    rows = [r for r in log.rows_since(60, "right") if "event" not in r]
    assert rows and all(len(r["v"]) == 2 for r in rows if "v" in r)
    dips = [r for r in rows if "v_low" in r and None not in r["v_low"] and min(r["v_low"]) < 40]
    assert dips and min(min(r["v_low"]) for r in dips) == 31.5
    assert any(r.get("v") == [48.0, 48.0] and r["v_low"] == [48.0, 48.0] for r in rows)
    supply = log.report(hours=1)["arms"]["right"]["supply_v"]
    assert supply["lowest"] == 31.5 and supply["highest"] == 48.0
    assert "supply voltage: highest 48.0 V, lowest 31.5 V" in report_text(log.report(hours=1))
    robot.power_off("right")
    assert arm.voltage == [None, None]


def test_loaded_hand_refuses_reaches_it_could_not_hold_for_a_minute(robot):
    robot.power_on("right")
    robot.go("ready", "right")
    robot.wait(20)
    unit = robot.units["right"]
    far, near = [0.62, -0.25, 0.85], [0.34, -0.25, 0.80]
    unit.move_tip(far)
    robot.wait(20)
    robot.go("ready", "right")
    robot.wait(20)
    assert unit.set_payload(1.0) == 1.0 and unit.status()["payload_kg"] == 1.0
    with pytest.raises(ValueError, match="1.0 kg in the hand"):
        unit.move_tip(far)
    unit.move_tip(near)
    robot.wait(20)
    empty = unit.kin.gravity_function()(unit.arm.command[:6])
    assert abs(unit.load_at(unit.arm.command[:6])[0]) > abs(empty[0]) + 1.5
    robot.set_heavy(True)
    try:
        unit.move_tip(far)
        robot.wait(20)
    finally:
        robot.set_heavy(False)
    assert unit.set_payload(99) == C.PAYLOAD_MAX_KG
    unit.set_payload(0)


def test_weigh_estimates_what_the_hand_holds_from_the_shoulder_motors(robot):
    robot.power_on("right")
    robot.go("ready", "right")
    robot.wait(20)
    time.sleep(0.5)
    unit = robot.units["right"]
    bus = robot.factory.buses[unit.arm.channel]
    q = unit.arm.measured[:6].copy()
    held = unit.kin.gravity_function()(q) + unit.kin.payload_torque(q, 0.8)
    assert unit.weigh() == 0.0
    with bus.lock:
        bus.torque = {i: float(held[i]) for i in range(6)}
    try:
        assert wait_until(lambda: abs(unit.arm.effort[0] - held[0]) < 0.05)
        assert unit.weigh() == pytest.approx(0.8, abs=0.05)
    finally:
        with bus.lock:
            bus.torque = {}
        unit.set_payload(0)


def test_loaded_hand_is_refused_over_the_api_and_letting_go_clears_the_weight():
    from rebot_teleop.server import create_app
    with TestClient(create_app(sim=True)) as client:
        assert wait_until(lambda: all(a["connected"] for a in client.get("/api/status").json()["arms"].values()))
        client.post("/api/power", json={"on": True, "arm": "right"})
        client.post("/api/move", json={"pose": "ready", "arm": "right"})
        assert client.post("/api/payload", json={"arm": "right", "kg": 1.5}).json()["payload_kg"] == 1.5
        refused = client.post("/api/move", json={"arm": "right", "tip_xyz": [0.62, -0.25, 0.85]})
        assert refused.status_code >= 400 and "kg in the hand" in refused.text
        client.post("/api/gripper", json={"arm": "right", "state": "open"})
        assert client.get("/api/payload").json()["right"] == 0.0
        assert client.post("/api/move", json={"arm": "right", "tip_xyz": [0.62, -0.25, 0.85]}).status_code == 200


def test_teleop_stops_a_loaded_hand_where_it_can_still_hold_for_a_minute(robot):
    robot.power_on()
    robot.go("ready")
    robot.wait(20)
    teleop = Teleop(robot)
    unit = robot.units["right"]
    arm = unit.arm

    def reach(kg):
        unit.set_payload(0)
        robot.go("ready", "right")
        robot.wait(20)
        unit.set_payload(kg)
        hand = {"p": [0.0, 1.0, -0.3], "q": [0, 0, 0, 1], "grip": 0.0, "trigger": 0.0}
        teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
        time.sleep(0.1)
        hand["grip"] = 1.0
        for _ in range(10):
            teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
            time.sleep(0.01)
        for _ in range(80):
            hand["p"] = [0.0, 1.05, -0.65]
            teleop.push_hands({"yaw": 0.0, "hands": {"right": hand}})
            time.sleep(0.01)
        time.sleep(0.5)
        return abs(unit.load_at(arm.target[:6])[0]), teleop.status()["hands"]["right"]["reach_limited"]

    try:
        free_load, free_limited = reach(0.0)
        held_load, held_limited = reach(2.0)
    finally:
        teleop.stop()
        unit.set_payload(0)
    assert free_load > 9.0 and not free_limited
    assert held_limited and held_load <= unit.reach_limit[0] + 0.5


def test_grip_strength_sets_how_hard_the_gripper_holds_what_it_closes_on(robot):
    robot.power_on()
    unit = robot.units["right"]
    arm, bus = unit.arm, robot.factory.buses[unit.arm.channel]
    kp = C.GAINS[6][0]

    def squeeze(strength):
        arm.grip(C.GRIPPER_WIDTH_M)
        bus.held.discard(6)
        assert wait_until(lambda: arm.width > 0.045)
        robot.set_grip(strength)
        arm.grip(0.0)
        assert wait_until(lambda: arm.width < 0.03)
        bus.held.add(6)
        peak = 0.0
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            peak = max(peak, abs(arm.command[6] - arm.measured[6]) * kp)
            time.sleep(0.002)
        assert arm.gripping and arm.status()["gripping"]
        return peak, arm.status()["grip_nm"], arm.width

    full_peak, full_hold, _ = squeeze(1.0)
    soft_peak, soft_hold, width = squeeze(0.3)
    assert full_hold == pytest.approx(kp * math.radians(C.GRIPPER_HOLD_LEAD_DEG), rel=0.5)
    assert soft_hold < 0.5 * full_hold and soft_peak < 0.6 * full_peak
    assert width > 0.01
    assert robot.set_grip(5)["grip"] == 1.0 and robot.set_grip(0)["grip"] == C.GRIP_MIN
    bus.held.discard(6)
    arm.grip(C.GRIPPER_WIDTH_M)
    assert wait_until(lambda: arm.width > 0.045 and not arm.gripping)
    assert C.load("limits.json", {})["grip"] == C.GRIP_MIN


def test_teleop_glide_reaches_the_goal_and_stop_ends_it(robot):
    robot.power_on()
    robot.go("ready", "left")
    robot.wait(20)
    arm = robot.units["left"].arm
    start = arm.command[:6].copy()
    goal = start + np.radians([0, 0, 12, 0, 0, 0])
    arm.servo(goal, 0.05)
    assert wait_until(lambda: np.max(np.abs(arm.q - goal)) < math.radians(0.5), 5)
    far = start.copy()
    arm.servo(far, 0.5)
    time.sleep(0.15)
    robot.stop()
    held = arm.target[:6].copy()
    time.sleep(0.7)
    assert np.allclose(arm.target[:6], held)
    assert np.max(np.abs(arm.q - held)) < math.radians(0.5)
    assert abs(arm.q[2] - far[2]) > math.radians(4)


def test_recorded_motion_plays_back_smoothly_and_keeps_the_gripper_command(robot):
    from rebot_teleop.sequencer import condition, TRACK_DT, TRACK_EASE_S
    rng = np.random.default_rng(3)
    t = np.cumsum(np.full(150, 0.02) + rng.uniform(-0.004, 0.004, 150))
    clean = np.stack([20 * np.sin(1.5 * t + i) for i in range(6)] + [0.02 + 0.02 * np.sin(t)], axis=1)
    noisy = clean + np.concatenate([rng.normal(0, 0.05, (150, 6)), np.zeros((150, 1))], axis=1)
    grid, rows = condition(t, noisy, smooth=0.06)
    speed = np.diff(rows[:, :6], axis=0) / TRACK_DT
    accel = np.diff(speed, axis=0) / TRACK_DT
    assert np.allclose(rows[0], noisy[0]) and np.allclose(rows[-1], noisy[-1])
    assert np.abs(speed[0]).max() < 1.0 and np.abs(speed[-1]).max() < 1.0
    assert np.abs(accel).max() < 1500
    middle = (grid > 0.6) & (grid < grid[-1] - 0.6)
    truth = np.stack([np.interp(grid[middle] - TRACK_EASE_S + t[0], t, clean[:, i]) for i in range(6)], axis=1)
    assert np.abs(rows[middle][:, :6] - truth).max() < 0.6
    seq = Sequencer(robot)
    robot.power_on()
    arm = robot.units["left"].arm
    robot.factory.buses["can1"].held.add(6)
    arm.grip(0.0)
    time.sleep(0.5)
    assert arm.width > 0.03 and arm.width_goal == pytest.approx(0.0, abs=1e-6)
    step = seq.capture("left")
    assert step["grippers"]["left"] == pytest.approx(0.0, abs=1e-4)
    robot.factory.buses["can1"].held.discard(6)


def test_sequence_edits_do_not_need_the_motion_data(robot):
    seq = Sequencer(robot)
    robot.power_on()
    seq.save({"name": "edits", "steps": []})
    seq.add("edits", seq.capture())
    seq.start_recording()
    time.sleep(0.3)
    seq.stop_recording("edits")
    seq.add("edits", {"type": "gripper", "arm": "both", "width": 0.0})
    light = seq.light(seq.get("edits"))
    assert [s["type"] for s in light["steps"]] == ["pose", "track", "gripper"]
    assert "t" not in light["steps"][1] and light["steps"][1]["samples"] > 5 and light["steps"][1]["arm_names"] == ["left", "right"]
    seq.edit("edits", {"op": "move", "index": 2, "to": 0})
    seq.edit("edits", {"op": "set", "index": 2, "fields": {"speed": 9, "label": "wave", "t": []}})
    seq.edit("edits", {"op": "duplicate", "index": 0})
    seq.edit("edits", {"op": "delete", "index": 2})
    seq.edit("edits", {"op": "meta", "loop": True})
    data = seq.get("edits")
    assert [s["type"] for s in data["steps"]] == ["gripper", "gripper", "track"] and data["loop"]
    assert data["steps"][2]["speed"] == 3.0 and data["steps"][2]["label"] == "wave" and len(data["steps"][2]["t"]) > 5
    with pytest.raises(ValueError):
        seq.edit("edits", {"op": "delete", "index": 9})
    robot.units["left"].arm.grip(C.GRIPPER_WIDTH_M)
    robot.units["right"].arm.grip(C.GRIPPER_WIDTH_M)
    assert wait_until(lambda: all(u.arm.width > 0.045 for u in robot.units.values()), 5)
    seq.play("edits", loop=False)
    assert wait_until(lambda: all(u.arm.width < 0.005 for u in robot.units.values()), 10)
    assert wait_until(lambda: seq.playing is None, 30)
    assert seq.progress is None
    seq.delete("edits")


def test_a_pose_can_leave_a_joint_where_it_is(robot, monkeypatch):
    robot.power_on()
    unit = robot.units["left"]
    robot.go("ready", "left")
    robot.wait(20)
    before = unit.arm.q.copy()
    target = [round(math.degrees(v), 2) + 5.0 for v in before]
    monkeypatch.setitem(C.POSES_DEG, "park", {"left": target[:5] + [None], "right": [None] * 6})
    assert C.pose_numbers("park", "right") == [0.0] * 6
    still = robot.units["right"].arm.q.copy()
    robot.go("park")
    robot.wait(20)
    assert np.allclose(np.degrees(unit.arm.q[:5]), target[:5], atol=0.5)
    assert abs(unit.arm.q[5] - before[5]) < math.radians(0.5)
    assert np.allclose(robot.units["right"].arm.q, still, atol=math.radians(0.5))


def test_planner_finds_a_way_from_a_folded_pose(robot):
    robot.power_on()
    unit = robot.units["right"]
    folded = np.radians([0.0, 0.0, 0.0, 0.5, 0.0, 0.0])
    unit.arm.move(np.clip(folded, unit.arm.lower, unit.arm.upper), 120.0)
    robot.wait(30)
    seed = unit.arm.command[:6].copy()
    keep = unit.kin.pose(seed)[1]
    ready_tip = unit.kin.pose(np.radians(C.pose_numbers("ready", "right")))[0]
    for offset in ([0.0, 0.0, 0.0], [0.05, -0.05, -0.05], [-0.03, 0.04, 0.05]):
        q, err, _ = unit.solve(ready_tip + np.array(offset), keep, seed)
        assert err < 0.01
        assert np.all(q >= unit.arm.lower - 1e-9) and np.all(q <= unit.arm.upper + 1e-9)


def test_held_joint_does_not_slow_down_moves(robot):
    unit = robot.units["left"]
    bus = robot.factory.buses[unit.arm.channel]
    unit.arm.joint_memory = np.radians([0.0, 0.0, 0.0, 0.0, 0.0, -174.0])
    bus.dead = {6}
    unit.arm.without = {5}
    unit.arm.close()
    assert wait_until(lambda: unit.arm.connected, timeout=5)
    assert unit.arm.held == [5]
    assert robot.power_on("left")["powered"] == ["left"]
    goal = unit.arm.q.copy()
    goal[0] += math.radians(10)
    goal[5] = math.radians(179.0)
    duration = unit.arm.move(goal, 45.0)
    assert duration < 1.5
    bus.dead = set()
    unit.arm.without = set()
    robot.power_off("left")
    unit.arm.close()
    assert wait_until(lambda: unit.arm.connected, timeout=5)


def test_gripper_reading_that_jumps_a_whole_turn_keeps_position_and_still_moves(robot):
    unit = robot.units["left"]
    bus = robot.factory.buses[unit.arm.channel]
    assert robot.power_on("left")["powered"] == ["left"]
    unit.arm.grip(0.0)
    assert wait_until(lambda: unit.arm.width < 0.002, timeout=5)
    with bus.lock:
        bus.pos[6] += 2 * math.pi
        bus.goal[6] += 2 * math.pi
    time.sleep(0.25)
    assert unit.arm.turn_fixes >= 1
    assert unit.arm.width < 0.002
    unit.arm.grip(C.GRIPPER_WIDTH_M)
    assert wait_until(lambda: unit.arm.width > 0.045, timeout=5)
    with bus.lock:
        bus.pos[6] -= 2 * math.pi
        bus.goal[6] -= 2 * math.pi
    time.sleep(0.25)
    assert unit.arm.width > 0.045
    unit.arm.grip(0.0)
    assert wait_until(lambda: unit.arm.width < 0.002, timeout=5)
