import asyncio
import math
import time
from types import SimpleNamespace
import numpy as np
import pinocchio as pin
import pytest
from rebot_teleop.rs_arm import RSArm




class FakeMotor:
    def __init__(self, bus, motor_id):
        self.bus = bus
        self.id = motor_id
        self.pos = 0.0
        self.stuck = False
        self.temperature = 35.0
        self.commands = []

    def robstride_ping_host_id(self, host, timeout):
        return self.id, host

    def robstride_get_param_f32(self, param, timeout):
        assert param == 0x7019
        return self.pos

    def ensure_mode(self, mode, timeout):
        self.bus.log.append(("mode", self.id))

    def send_mit(self, pos, vel, kp, kd, tau):
        self.commands.append(pos)
        self.bus.log.append(("mit", self.id))
        if self.bus.enabled and not self.stuck:
            self.pos = pos

    def get_state(self):
        return SimpleNamespace(pos=self.pos, vel=0.0, torq=0.1, t_mos=self.temperature)


class FakeBus:
    def __init__(self, channel):
        self.channel = channel
        self.log = []
        self.enabled = False
        self.motors = {}
        self.closed = False

    def add_robstride_motor(self, motor_id, host, model):
        motor = FakeMotor(self, motor_id)
        self.motors[motor_id] = motor
        return motor

    def enable_all(self):
        self.enabled = True
        self.log.append(("enable_all",))

    def disable_all(self):
        self.enabled = False
        self.log.append(("disable_all",))

    def close(self):
        self.closed = True


def make_arm(**options):
    buses = []

    def factory(channel):
        bus = FakeBus(channel)
        buses.append(bus)
        return bus

    return RSArm(controller_factory=factory, **options), buses


def wait_for(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(.01)
    return condition()


def test_connect_and_zero_check_never_enable_or_command():
    arm, buses = make_arm()
    arm.connect()
    bus = buses[0]
    assert arm.connected and not arm.zeroed and not arm.torque
    assert not any(entry[0] in {"mit", "enable_all", "mode"} for entry in bus.log)
    bus.motors[2].pos = math.radians(20)
    with pytest.raises(ValueError, match="Joints 2"):
        arm.confirm_zero()
    assert not arm.zeroed
    bus.motors[2].pos = math.radians(2)
    arm.confirm_zero()
    assert arm.zeroed
    assert not any(entry[0] in {"mit", "enable_all"} for entry in bus.log)
    with pytest.raises(ValueError):
        arm.write(np.zeros(6), .02)
    arm.close()


def test_enable_holds_measured_pose_without_a_jump():
    arm, buses = make_arm()
    arm.connect()
    bus = buses[0]
    bus.motors[1].pos = math.radians(3)
    arm.confirm_zero()
    arm.enable()
    try:
        first_enable = bus.log.index(("enable_all",))
        for motor_id in range(1, 8):
            assert ("mode", motor_id) in bus.log[:first_enable]
        assert bus.motors[1].commands[0] == pytest.approx(math.radians(3))
        assert wait_for(lambda: len(bus.motors[1].commands) > 20)
        assert np.allclose(bus.motors[1].commands, math.radians(3))
    finally:
        arm.close()


def test_motion_is_speed_limited_and_stop_freezes():
    arm, buses = make_arm(speed_deg_s=20.0)
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    try:
        arm.enabled = True
        target = np.radians([0, 40, 0, 0, 0, 0])
        start = time.monotonic()
        arm.write(target, .02)
        time.sleep(.5)
        elapsed = time.monotonic() - start
        assert arm.moving
        assert 0 < arm.command[1] <= math.radians(20) * elapsed + 1e-6
        moving_at = arm.command[1]
        rate = arm.rate[1]
        arm.stop()
        assert wait_for(lambda: not arm.moving, 1)
        braked = arm.command[1] - moving_at
        assert 0 <= braked <= rate * rate / (2 * math.radians(240)) + math.radians(.5)
        frozen = arm.command.copy()
        time.sleep(.15)
        assert np.allclose(arm.command, frozen)
        with pytest.raises(ValueError):
            arm.write(target, .02)
    finally:
        arm.close()


def test_limits_are_refused_not_clamped():
    arm, _ = make_arm()
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    try:
        arm.enabled = True
        for q in (np.radians([0, -5, 0, 0, 0, 0]), np.radians([0, 0, -4, 0, 0, 0]), np.radians([0, 0, 0, 95, 0, 0]), np.radians([170, 0, 0, 0, 0, 0])):
            with pytest.raises(ValueError, match="limit"):
                arm.write(q, .02)
        with pytest.raises(ValueError):
            arm.write([0, 0, 0, 0, 0, float("nan")], .02)
        assert np.allclose(arm.target[:6], 0)
    finally:
        arm.close()


def test_blocked_joint_faults_and_holds_where_it_is():
    arm, buses = make_arm(speed_deg_s=60.0)
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    try:
        buses[0].motors[3].stuck = True
        arm.enabled = True
        arm.write(np.radians([0, 0, 60, 0, 0, 0]), .02)
        assert wait_for(lambda: arm.fault is not None, 3)
        assert "Joint 3" in arm.fault
        assert np.allclose(arm.target, arm.measured)
        assert arm.torque and not arm.enabled
    finally:
        arm.close()


def test_hot_motor_faults():
    arm, buses = make_arm()
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    try:
        buses[0].motors[5].temperature = 80.0
        assert wait_for(lambda: arm.status()["temperature_warning"] is not None, 3)
        assert arm.fault is None
        buses[0].motors[5].temperature = 105.0
        assert wait_for(lambda: arm.fault is not None, 3)
        assert "Joint 5" in arm.fault and "100 C" in arm.fault
        with pytest.raises(ValueError, match="still"):
            arm.check_cool()
        buses[0].motors[5].temperature = 60.0
        assert wait_for(lambda: arm.temperatures[4] == 60.0, 3)
        arm.check_cool()
    finally:
        arm.close()


def test_gripper_maps_width_to_angle():
    arm, _ = make_arm(gripper_open_deg=90.0)
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    try:
        arm.enabled = True
        arm.grip(.05)
        assert arm.target[6] == pytest.approx(math.radians(90))
        arm.grip(0)
        assert arm.target[6] == 0
        with pytest.raises(ValueError):
            arm.grip(.2)
    finally:
        arm.close()


def test_close_keeps_holding_and_release_turns_torque_off():
    arm, buses = make_arm()
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    arm.close()
    assert ("disable_all",) not in buses[0].log and buses[0].closed
    arm2, buses2 = make_arm()
    arm2.connect()
    arm2.confirm_zero()
    arm2.enable()
    arm2.release()
    assert ("disable_all",) in buses2[0].log and not arm2.torque
    arm2.close()


def test_acceleration_is_limited_and_velocity_is_fed_forward():
    arm, buses = make_arm(speed_deg_s=30.0)
    arm.connect()
    arm.confirm_zero()
    arm.enable()
    try:
        arm.enabled = True
        arm.jog(0, 1, math.radians(30))
        time.sleep(.6)
        rates = []
        for _ in range(20):
            rates.append(arm.rate[0])
            time.sleep(.005)
        assert max(rates) <= math.radians(30) + 1e-6
        assert arm.rate[0] > 0
        assert np.allclose(arm.target[1:6], arm.command[1:6])
        commands = np.array(buses[0].motors[1].commands)
        steps = np.diff(commands)
        assert np.max(np.abs(np.diff(steps))) <= math.radians(120) * arm.period ** 2 * 1.5 + 1e-9
        arm.stop()
        assert wait_for(lambda: not arm.moving, 1)
        assert arm.rate[0] == 0
    finally:
        arm.close()


def test_gravity_feedforward_is_sent_and_capped():
    calls = []

    def gravity(q):
        calls.append(np.array(q))
        return np.array([0, 50.0, 6.0, 2.0, 0, 0])

    arm, buses = make_arm(gravity=gravity)
    torques = []
    arm.connect()
    arm.confirm_zero()
    motor = buses[0].motors[2]
    original = motor.send_mit

    def spy(pos, vel, kp, kd, tau):
        torques.append(tau)
        original(pos, vel, kp, kd, tau)

    motor.send_mit = spy
    buses[0].motors[3].send_mit = lambda pos, vel, kp, kd, tau: torques.append(("j3", tau))
    arm.enable()
    try:
        assert wait_for(lambda: len(torques) > 10)
        assert calls
        assert max(t for t in torques if not isinstance(t, tuple)) == pytest.approx(12.0)
        assert any(t == ("j3", pytest.approx(6.0)) for t in torques if isinstance(t, tuple))
    finally:
        arm.close()


def wrapped_arm(raw_deg):
    buses = []

    def factory(channel):
        bus = FakeBus(channel)
        original = bus.add_robstride_motor

        def add(motor_id, host, model):
            motor = original(motor_id, host, model)
            motor.pos = math.radians(raw_deg[motor_id - 1])
            return motor

        bus.add_robstride_motor = add
        buses.append(bus)
        return bus

    return RSArm(controller_factory=factory), buses


def test_a_joint_resting_past_the_encoder_wrap_reads_inside_its_range_and_holds_still():
    raw = [83.9, -182.1, 161.5, 6.3, -19.1, -178.5, 30.0]
    arm, buses = wrapped_arm(raw)
    arm.connect()
    assert math.degrees(arm.offset[1]) == pytest.approx(360.0)
    assert np.count_nonzero(arm.offset) == 1
    assert math.degrees(arm.q[1]) == pytest.approx(177.9, abs=0.01)
    arm.confirm_zero(True)
    arm.enable()
    try:
        time.sleep(0.05)
        motor = buses[0].motors[2]
        assert math.degrees(motor.commands[0]) == pytest.approx(-182.1, abs=0.01)
        assert math.degrees(motor.commands[-1]) == pytest.approx(-182.1, abs=0.01)
        assert math.degrees(motor.pos) == pytest.approx(-182.1, abs=0.01)
        assert math.degrees(arm.q[1]) == pytest.approx(177.9, abs=0.01)
        assert arm.fault is None
        arm.enabled = True
        arm.write(np.radians([83.9, 172.0, 161.5, 6.3, -19.1, -178.5]), 0.02)
        assert wait_for(lambda: abs(math.degrees(motor.pos) + 188.0) < 0.05, timeout=3.0)
        assert math.degrees(arm.q[1]) == pytest.approx(172.0, abs=0.05)
    finally:
        arm.running.clear()


def test_joints_inside_their_range_get_no_offset_and_true_outliers_are_still_refused():
    arm, _ = wrapped_arm([-88.1, 176.7, 160.1, 11.0, 1.9, 153.6, 30.0])
    arm.connect()
    assert not np.any(arm.offset)
    arm, _ = wrapped_arm([0.0, 90.0, 300.0, 0.0, 0.0, 0.0, 0.0])
    arm.connect()
    assert not np.any(arm.offset)
    with pytest.raises(ValueError, match="outside their range"):
        arm.confirm_zero(True)


def test_a_motor_that_lost_its_turn_count_is_reread_before_powering_on():
    raw = [83.9, -182.1, 161.5, 6.3, -19.1, -178.5, 30.0]
    arm, buses = wrapped_arm(raw)
    arm.connect()
    assert math.degrees(arm.offset[1]) == pytest.approx(360.0)
    buses[0].motors[2].pos = math.radians(177.9)
    arm.read_positions()
    assert math.degrees(arm.q[1]) == pytest.approx(537.9, abs=0.01)
    arm.confirm_zero(True)
    arm.enable()
    try:
        assert np.count_nonzero(arm.offset) == 0
        assert math.degrees(arm.q[1]) == pytest.approx(177.9, abs=0.01)
        assert math.degrees(arm.command[1]) == pytest.approx(177.9, abs=0.01)
    finally:
        arm.close()


def test_an_open_gripper_resting_past_the_encoder_wrap_reads_open_and_closes_the_right_way():
    arm, buses = wrapped_arm([0.0, 90.0, 90.0, 0.0, 0.0, 0.0, -90.2])
    arm.set_gripper_range(0.0, math.radians(270.0))
    arm.connect()
    assert math.degrees(arm.offset[6]) == pytest.approx(360.0)
    assert np.count_nonzero(arm.offset) == 1
    arm.confirm_zero(True)
    arm.enable()
    try:
        motor = buses[0].motors[7]
        assert arm.width == pytest.approx(0.05, abs=1e-4)
        assert math.degrees(motor.commands[-1]) == pytest.approx(-90.2, abs=0.01)
        arm.enabled = True
        arm.grip(0)
        assert wait_for(lambda: abs(math.degrees(motor.pos) + 360.0) < 0.5, timeout=4.0)
        assert arm.width == pytest.approx(0.0, abs=1e-3)
    finally:
        arm.close()


def test_saying_the_gripper_is_open_shifts_its_reading_by_whole_turns_without_moving_it():
    arm, buses = wrapped_arm([0.0, 90.0, 90.0, 0.0, 0.0, 0.0, -1.7])
    arm.set_gripper_range(0.0, math.radians(270.0))
    arm.connect()
    assert not np.any(arm.offset)
    arm.confirm_zero(True)
    arm.enable()
    try:
        motor = buses[0].motors[7]
        time.sleep(0.05)
        sent = len(motor.commands)
        assert math.degrees(arm.set_gripper_state("open")) == pytest.approx(358.3, abs=0.01)
        time.sleep(0.05)
        assert len(motor.commands) > sent
        assert math.degrees(motor.commands[-1]) == pytest.approx(-1.7, abs=0.01)
        assert math.degrees(motor.pos) == pytest.approx(-1.7, abs=0.01)
        assert math.degrees(arm.set_gripper_state("closed")) == pytest.approx(-1.7, abs=0.01)
        with pytest.raises(ValueError):
            arm.set_gripper_state("ajar")
    finally:
        arm.close()
