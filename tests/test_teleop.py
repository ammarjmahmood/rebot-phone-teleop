import asyncio
import math
import time
from types import SimpleNamespace
import numpy as np
import pinocchio as pin
import pytest
from fastapi.testclient import TestClient
from rebot_teleop.controller import TeleopController
from rebot_teleop.models import Principal
from rebot_teleop.node import TeleopNode
from rebot_teleop.rs_arm import RSArm
from rebot_teleop.server import create_app
from rebot_teleop.sources import PushSource


OWNER = Principal(id="owner", role="owner", local=True)


class FakeMotor:
    def __init__(self, bus, motor_id):
        self.bus = bus
        self.id = motor_id
        self.pos = 0.0
        self.stuck = False
        self.temperature = 35.0

    def robstride_ping_host_id(self, host, timeout):
        return self.id, host

    def robstride_get_param_f32(self, param, timeout):
        return self.pos

    def ensure_mode(self, mode, timeout):
        self.bus.log.append(("mode", self.id))

    def send_mit(self, pos, vel, kp, kd, tau):
        self.bus.log.append(("mit", self.id))
        if self.bus.enabled and not self.stuck:
            self.pos = pos

    def get_state(self):
        return SimpleNamespace(pos=self.pos, vel=0.0, torq=0.1, t_mos=self.temperature)


class FakeBus:
    def __init__(self, channel):
        self.log = []
        self.enabled = False
        self.motors = {}

    def add_robstride_motor(self, motor_id, host, model):
        self.motors[motor_id] = FakeMotor(self, motor_id)
        return self.motors[motor_id]

    def enable_all(self):
        self.enabled = True
        self.log.append(("enable_all",))

    def disable_all(self):
        self.enabled = False
        self.log.append(("disable_all",))

    def close(self):
        pass


def make_node(path):
    buses = []

    def factory(channel):
        bus = FakeBus(channel)
        buses.append(bus)
        return bus

    holder = {}

    def build(name="arm", channel="can0"):
        arm = RSArm(channel, controller_factory=factory, gripper_open_deg=270.0, speed_deg_s=60.0)
        node = TeleopNode(path if name == "arm" else path / name, arm=arm)
        node.controllers = {
            "app": TeleopController(node, "app", PushSource(), "align"),
            "quest": TeleopController(node, "quest", PushSource(), "webxr"),
            "hebi": TeleopController(node, "hebi", PushSource(), "calibrated"),
        }
        holder["node"] = node
        holder[name] = node
        return node

    return build, buses, holder


def yaw(angle):
    return [math.cos(angle / 2), 0.0, math.sin(angle / 2), 0.0]


async def ready(path, pose_deg=(0, 40, 60, 0, 20, 0)):
    build, buses, _ = make_node(path)
    node = build()
    await node.start()
    await node.power_on(False)
    node.arm.enabled = True
    node.arm.write(np.radians(pose_deg), 0.02)
    for _ in range(300):
        if not node.arm.moving:
            break
        await asyncio.sleep(0.02)
    node.arm.stop()
    return node, buses


async def settle(node, steps=200):
    for _ in range(steps):
        await asyncio.sleep(0.02)
        if not node.arm.moving:
            return


def test_connect_reads_without_enabling(tmp_path):
    build, buses, _ = make_node(tmp_path)
    node = build()
    asyncio.run(node.connect())
    assert node.arm.connected and not node.arm.torque
    assert not any(entry[0] in {"mit", "enable_all", "mode"} for entry in buses[0].log)
    node.arm.close()


def test_power_on_anywhere_and_home(tmp_path):
    async def scenario():
        build, buses, _ = make_node(tmp_path)
        node = build()
        await node.start()
        try:
            buses[0].motors[6].pos = math.radians(170)
            buses[0].motors[3].pos = math.radians(40)
            buses[0].motors[4].pos = math.radians(90.3)
            await node.power_on(True)
            assert node.fault is None
            assert np.allclose(node.arm.q, 0, atol=0.01)
            buses[0].motors[2].pos = 0.0
        finally:
            await node.close()

    asyncio.run(scenario())


def test_limits_refused_and_far_past_limit_blocks_home(tmp_path):
    async def scenario():
        node, buses = await ready(tmp_path)
        try:
            node.arm.enabled = True
            with pytest.raises(ValueError, match="limit"):
                node.arm.write(np.radians([0, -10, 0, 0, 0, 0]), 0.02)
            node.arm.measured[3] = math.radians(-105)
            with pytest.raises(ValueError, match="20.0 degrees past its limit"):
                node.planning_start()
        finally:
            await node.close()

    asyncio.run(scenario())


def test_app_alignment_translation_rotation_and_grip(tmp_path):
    async def scenario():
        node, buses = await ready(tmp_path)
        try:
            app = node.controllers["app"]
            app.source.push([0, 1, 0], yaw(0.0), {"b1": 0})
            app.set_view("behind")
            await app.start(OWNER)
            assert app.axes @ np.array([0, 0, -1.0]) == pytest.approx([1, 0, 0])
            assert app.axes @ np.array([-1.0, 0, 0]) == pytest.approx([0, 1, 0])
            start_tip, start_rotation = node.kin.pose(node.arm.command[:6])
            for _ in range(4):
                app.source.push([0, 1, 0], yaw(0.0), {"b1": 1})
                await asyncio.sleep(0.03)
            for step in range(1, 11):
                app.source.push([0, 1, -0.005 * step], yaw(0.02 * step), {"b1": 1})
                await asyncio.sleep(0.03)
            for _ in range(100):
                app.source.push([0, 1, -0.05], yaw(0.2), {"b1": 1})
                await asyncio.sleep(0.02)
                if not node.arm.moving:
                    break
            tip, rotation = node.kin.pose(node.arm.command[:6])
            moved = tip - start_tip
            turn = pin.log3(rotation @ start_rotation.T)
            from rebot_teleop.controller import ARKIT_CENTER
            from rebot_teleop.sources import rotation_from_quaternion
            expected = app.axes @ ((np.array([0, 1, -0.05]) + rotation_from_quaternion(yaw(0.2)) @ ARKIT_CENTER) - (np.array([0, 1, 0]) + ARKIT_CENTER))
            assert moved == pytest.approx(expected, abs=0.006)
            assert turn[2] == pytest.approx(0.2, abs=0.03)
            app.source.push([0, 1, -0.05], yaw(0.2), {"b1": 0, "grip": 0.0})
            await asyncio.sleep(0.06)
            assert app.mode is None and not node.clutch
            assert node.arm.target[6] == pytest.approx(node.arm.gripper_closed)
            app.source.push([0, 1, -0.05], yaw(0.2), {"b1": 0, "grip": 1.0})
            await asyncio.sleep(0.06)
            assert node.arm.target[6] == pytest.approx(math.radians(270))
        finally:
            await node.close()

    asyncio.run(scenario())


def test_quest_frame_and_relative_rotation(tmp_path):
    async def scenario():
        node, buses = await ready(tmp_path)
        try:
            quest = node.controllers["quest"]
            quest.set_view("front")
            assert quest.axes @ np.array([0, 0, -1.0]) == pytest.approx([-1, 0, 0])
            quest.set_view("behind")
            await quest.start(OWNER)
            start_tip = node.kin.pose(node.arm.command[:6])[0]
            for _ in range(4):
                quest.source.push([0, 1.2, 0], [1, 0, 0, 0], {"b1": 1})
                await asyncio.sleep(0.03)
            for step in range(1, 11):
                quest.source.push([0, 1.2 + 0.004 * step, 0], [1, 0, 0, 0], {"b1": 1})
                await asyncio.sleep(0.03)
            await settle(node)
            moved = node.kin.pose(node.arm.command[:6])[0] - start_tip
            assert moved == pytest.approx([0, 0, 0.04], abs=0.005)
        finally:
            await node.close()

    asyncio.run(scenario())


def test_hebi_calibration_handles_mirrored_data_and_buttons(tmp_path):
    async def scenario():
        node, buses = await ready(tmp_path)
        try:
            hebi = node.controllers["hebi"]
            hebi.source.push([0, 0, 0], [1, 0, 0, 0], {"b1": 0})
            await hebi.start(OWNER)
            for step, direction in (("up", [0, 0, -1]), ("forward", [1, 0, 0]), ("left", [0, 1, 0])):
                hebi.calibrate(step)
                for scale in (0, 0.05, 0.1):
                    hebi.source.push(np.array(direction) * scale, [1, 0, 0, 0], {"b1": 1})
                    await asyncio.sleep(0.04)
                hebi.source.push(np.array(direction) * 0.1, [1, 0, 0, 0], {"b1": 0})
                await asyncio.sleep(0.04)
            axes = hebi.axes
            assert np.linalg.det(axes) < 0
            for room, robot in (([1, 0, 0], [1, 0, 0]), ([0, 1, 0], [0, 1, 0]), ([0, 0, -1], [0, 0, 1])):
                assert axes @ np.array(room, dtype=float) == pytest.approx(robot)
            assert node.settings.get("hebi_axes") is not None
            hebi.source.push([0, 0, 0], [1, 0, 0, 0], {"b1": 0, "b3": 1})
            await asyncio.sleep(0.04)
            assert node.arm.target[6] == pytest.approx(node.arm.gripper_closed)
            hebi.source.push([0, 0, 0], [1, 0, 0, 0], {"b1": 0, "b2": 1})
            await asyncio.sleep(0.04)
            assert node.arm.target[6] == pytest.approx(math.radians(270))
        finally:
            await node.close()

    asyncio.run(scenario())


def test_stale_pose_stops_and_heartbeat_releases_control(tmp_path):
    async def scenario():
        node, buses = await ready(tmp_path)
        try:
            app = node.controllers["app"]
            app.source.push([0, 1, 0], yaw(0.0), {"b1": 1})
            await app.start(OWNER)
            for _ in range(5):
                app.source.push([0, 1, 0], yaw(0.0), {"b1": 1})
                await asyncio.sleep(0.03)
            assert app.mode == "motion" and node.clutch
            await asyncio.sleep(0.4)
            assert app.mode is None and not node.arm.enabled
            await asyncio.sleep(3.0)
            assert node.owner is None and not app.active
        finally:
            await node.close()

    asyncio.run(scenario())


def test_server_pairing_socket_autostart_and_quest_api(tmp_path, monkeypatch):
    build, buses, holder = make_node(tmp_path / "data")
    with TestClient(create_app(tmp_path / "data", tmp_path / ".env", node_factory=build)) as client:
        session = client.post("/api/session", json={}).json()
        assert client.post("/api/power", json={"home": False}).status_code == 200
        node = holder["node"]
        node.arm.enabled = True
        node.arm.write(np.radians([0, 40, 60, 0, 20, 0]), 0.02)
        for _ in range(300):
            if not node.arm.moving:
                break
            time.sleep(0.02)
        node.arm.stop()
        headers = {"authorization": "Bearer " + session["session"]}
        with client.websocket_connect("/ws/app", headers=headers) as socket:
            socket.receive_json()
            socket.send_json({"type": "pose", "p": [0, 1, 0], "q": yaw(0.0), "move": True})
            time.sleep(0.1)
            assert not node.controllers["app"].active
        client.post("/api/remote", json={"enabled": True})
        with client.websocket_connect("/ws/app", headers=headers) as socket:
            socket.receive_json()
            socket.send_json({"type": "pose", "p": [0, 1, 0], "q": yaw(0.0), "move": False, "grip": 1.0})
            socket.send_json({"type": "pose", "p": [0, 1, 0], "q": yaw(0.0), "move": False, "grip": 0.5})
            time.sleep(0.1)
            assert node.controllers["app"].active
            assert node.arm.target[6] == pytest.approx(math.radians(135), abs=0.02)
            start_tip = node.kin.pose(node.arm.command[:6])[0]
            for _ in range(4):
                socket.send_json({"type": "pose", "p": [0, 1, 0], "q": yaw(0.0), "move": True, "grip": 0.5})
                time.sleep(0.03)
            for step in range(1, 11):
                socket.send_json({"type": "pose", "p": [0, 1, -0.005 * step], "q": yaw(0.0), "move": True, "grip": 0.5})
                time.sleep(0.03)
            for _ in range(100):
                socket.send_json({"type": "pose", "p": [0, 1, -0.05], "q": yaw(0.0), "move": True, "grip": 0.5})
                time.sleep(0.02)
                if not node.arm.moving:
                    break
            assert (node.kin.pose(node.arm.command[:6])[0] - start_tip)[0] == pytest.approx(0.05, abs=0.006)
        for _ in range(50):
            if not node.controllers["app"].active:
                break
            time.sleep(0.02)
        assert not node.controllers["app"].active
        assert client.post("/api/quest/pose", json={"position": [0, 0, 0], "quaternion_wxyz": [1, 0, 0, 0]}).status_code == 409
        assert client.get("/quest").status_code == 200
        client.cookies.clear()
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/app", headers={"authorization": "Bearer forged"}) as socket:
                socket.receive_json()
        assert client.post("/api/power", json={"home": True}, headers={"x-forwarded-for": "10.0.0.2"}).status_code in {401, 403}


def test_power_on_refuses_joint_far_outside_range(tmp_path):
    async def scenario():
        build, buses, _ = make_node(tmp_path)
        node = build()
        await node.start()
        try:
            buses[0].motors[4].pos = math.radians(105)
            with pytest.raises(ValueError, match="Joints 4 read more than 15 degrees"):
                await node.power_on(True)
            assert not node.arm.torque
        finally:
            await node.close()

    asyncio.run(scenario())


def test_extra_hosts_and_wildcards(tmp_path, monkeypatch):
    monkeypatch.setenv("REBOT_TELEOP_ALLOWED_HOSTS", "arm.example.org,*.trycloudflare.com")
    build, buses, holder = make_node(tmp_path / "data")
    with TestClient(create_app(tmp_path / "data", tmp_path / ".env", node_factory=build)) as client:
        assert client.get("/health", headers={"host": "arm.example.org"}).status_code == 200
        assert client.get("/health", headers={"host": "quiet-river-1234.trycloudflare.com"}).status_code == 200
        assert client.get("/health", headers={"host": "evil.example.com"}).status_code == 403
        session = client.post("/api/session", json={}, headers={"host": "quiet-river-1234.trycloudflare.com"})
        assert session.status_code in {401, 409, 422, 500} or not session.json().get("local")


def test_pairing_codes_open_the_app(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    monkeypatch.setenv("REBOT_TELEOP_LAN", "1")
    monkeypatch.setattr("rebot_teleop.server.lan_addresses", lambda: [("wlan0", "192.168.1.20"), ("tailscale0", "100.89.54.6")])
    monkeypatch.setattr("rebot_teleop.server.tailscale_name", lambda: None)
    build, buses, holder = make_node(tmp_path / "data")
    with TestClient(create_app(tmp_path / "data", tmp_path / ".env", node_factory=build)) as client:
        client.post("/api/session", json={})
        result = client.post("/api/pair", json={}).json()
        labels = [a["label"] for a in result["addresses"]]
        assert labels == ["WiFi", "Tailscale"]
        link = urlparse(result["addresses"][1]["app_link"])
        assert link.scheme == "rebotteleop"
        query = parse_qs(link.query)
        assert query["url"] == ["http://100.89.54.6:8080"] and query["code"] == [result["code"]]
        assert result["addresses"][1]["qr"].startswith("data:image/svg+xml")


def pose_arm(node, degrees=(0, 40, 60, 0, 20, 0)):
    node.arm.enabled = True
    node.arm.write(np.radians(degrees), 0.02)
    for _ in range(300):
        if not node.arm.moving:
            break
        time.sleep(0.02)
    node.arm.stop()


def test_two_arms_quest_hands_app_switch_and_stop(tmp_path):
    build, buses, holder = make_node(tmp_path / "data")
    arms = {"left": "can1", "right": "can2"}
    with TestClient(create_app(tmp_path / "data", tmp_path / ".env", arms=arms, node_factory=build)) as client:
        session = client.post("/api/session", json={}).json()
        state = client.get("/api/state").json()
        assert set(state["arms"]) == {"left", "right"}
        assert client.post("/api/power", json={"home": False}).status_code == 409
        for name in arms:
            assert client.post("/api/power", json={"arm": name, "home": False}).status_code == 200
        left, right = holder["left"], holder["right"]
        assert left.arm.channel == "can1" and right.arm.channel == "can2"
        pose_arm(left)
        pose_arm(right)
        left_start = left.kin.pose(left.arm.command[:6])[0]
        right_start = right.kin.pose(right.arm.command[:6])[0]
        assert client.post("/api/quest/start", json={"view": "behind"}).json()["hands"] == ["left", "right"]
        still = {"position": [0, 1.2, 0], "quaternion_wxyz": [1, 0, 0, 0]}
        for _ in range(4):
            client.post("/api/quest/pose", json={"hands": {"left": {**still, "inputs": {"b1": 1}}, "right": {**still, "inputs": {"b1": 0}}}})
            time.sleep(0.03)
        for step in range(1, 11):
            client.post("/api/quest/pose", json={"hands": {"left": {"position": [0, 1.2 + 0.004 * step, 0], "quaternion_wxyz": [1, 0, 0, 0], "inputs": {"b1": 1}}, "right": {"position": [0, 1.2 + 0.004 * step, 0], "quaternion_wxyz": [1, 0, 0, 0], "inputs": {"b1": 0}}}})
            time.sleep(0.03)
        for _ in range(100):
            client.post("/api/quest/pose", json={"hands": {"left": {"position": [0, 1.24, 0], "quaternion_wxyz": [1, 0, 0, 0], "inputs": {"b1": 1}}, "right": {**still, "inputs": {"b1": 0}}}})
            time.sleep(0.02)
            if not left.arm.moving:
                break
        assert (left.kin.pose(left.arm.command[:6])[0] - left_start) == pytest.approx([0, 0, 0.04], abs=0.006)
        assert np.allclose(right.kin.pose(right.arm.command[:6])[0], right_start, atol=1e-4)
        client.post("/api/quest/stop", json={})
        client.post("/api/remote", json={"enabled": True})
        headers = {"authorization": "Bearer " + session["session"]}
        with client.websocket_connect("/ws/app", headers=headers) as socket:
            status = socket.receive_json()
            assert status["arm"] == "left" and status["arms"] == ["left", "right"]
            socket.send_json({"type": "pose", "p": [0, 1, 0], "q": yaw(0.0), "move": True})
            time.sleep(0.1)
            assert left.controllers["app"].active and not right.controllers["app"].active
            socket.send_json({"type": "arm", "arm": "right"})
            socket.send_json({"type": "pose", "p": [0, 1, 0], "q": yaw(0.0), "move": True})
            time.sleep(0.15)
            assert right.controllers["app"].active and not left.controllers["app"].active
            for _ in range(5):
                status = socket.receive_json()
            assert status["arm"] == "right"
        assert client.post("/api/stop", json={}).status_code == 200
        assert left.estopped and right.estopped


def test_gist_bookmark_creates_then_updates(tmp_path):
    from rebot_teleop.server import GistBookmark
    calls = []

    def fake(method, url, body):
        calls.append((method, url, body))
        return {"id": "abc123"}

    gist = GistBookmark("token", "", tmp_path / "gist_id")
    gist._request = fake
    gist.publish("https://quiet-river-1234.trycloudflare.com")
    assert calls[0][0] == "POST" and calls[0][2]["public"] is False
    assert "https://quiet-river-1234.trycloudflare.com/quest" in calls[0][2]["files"]["rebot-teleop.md"]["content"]
    assert (tmp_path / "gist_id").read_text() == "abc123"
    gist.publish("https://other-name-99.trycloudflare.com")
    assert calls[1][0] == "PATCH" and calls[1][1].endswith("/gists/abc123")
    assert gist.status == "https://gist.github.com/abc123"
