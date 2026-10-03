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
        buses[0].motors[5].temperature = 95.0
        assert wait_for(lambda: arm.fault is not None, 3)
        assert "temperature" in arm.fault
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
