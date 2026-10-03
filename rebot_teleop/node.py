import asyncio
import math
import time
from pathlib import Path
import numpy as np
from rebot_teleop.kinematics import Kinematics
from rebot_teleop.rs_arm import RSArm
from rebot_teleop.settings import Settings


ASSETS = Path(__file__).resolve().parents[1] / "assets"
HOME_SPEED = math.radians(20.0)
HOME_ACCELERATION = math.radians(60.0)


class TeleopNode:
    def __init__(self, data_dir: Path, channel="can0", arm=None):
        self.settings = Settings(Path(data_dir) / "settings.json")
        self.kin = Kinematics(ASSETS / "rebot_rs.urdf")
        self.arm = arm or RSArm(channel, gripper_open_deg=270.0, gravity=self.kin.gravity_function())
        self.kin.lower = self.arm.lower.copy()
        self.kin.upper = self.arm.upper.copy()
        saved = self.settings.get("gripper_range")
        if saved:
            self.arm.set_gripper_range(math.radians(saved["closed"]), math.radians(saved["open"]))
        self.controllers = {}
        self.owner = None
        self.clutch = False
        self.heartbeat_at = 0.0
        self.generation = 0
        self.estopped = False
        self.fault = None
        self.remote_until = 0.0
        self.motion = None
        self.monitor_task = None

    async def start(self):
        self.monitor_task = asyncio.create_task(self.monitor())
        await self.connect()

    async def close(self):
        for controller in self.controllers.values():
            await controller.stop("Service stopped")
        await self.stop("Service stopped")
        if self.monitor_task:
            self.monitor_task.cancel()
        await asyncio.to_thread(self.arm.close)

    async def connect(self):
        if self.arm.connected:
            return
        try:
            await asyncio.to_thread(self.arm.connect)
            self.fault = None
        except Exception as error:
            self.fault = f"Arm not reachable on {self.arm.channel}: {error}"

    async def monitor(self):
        while True:
            await asyncio.sleep(0.025)
            now = time.monotonic()
            if self.owner and now - self.heartbeat_at > (0.35 if self.clutch or self.arm.moving else 3.0):
                await self.stop("Control signal lost")
            if self.arm.fault and self.fault != self.arm.fault:
                await self.stop(self.arm.fault)

    def require_ready(self):
        if self.estopped:
            raise ValueError("Stop is latched; reset it at the computer")
        if not self.arm.connected:
            raise ValueError("The arm is not connected")
        if not self.arm.torque:
            raise ValueError("Power the arm on first")
        if self.arm.fault:
            raise ValueError(self.arm.fault)

    def authorize(self, principal):
        if principal.role == "viewer":
            raise ValueError("An operator role is required")
        if not principal.local and time.monotonic() > self.remote_until:
            raise ValueError("Allow phone motion at the computer first")

    async def takeover(self, principal):
        self.authorize(principal)
        self.require_ready()
        await self.stop("Control taken")
        self.owner = principal.id
        self.heartbeat_at = time.monotonic()
        self.fault = None
        return self.generation

    async def stop(self, reason="Stopped", latch=False):
        self.generation += 1
        self.arm.stop()
        self.owner = None
        self.clutch = False
        self.estopped = self.estopped or latch
        self.fault = reason if latch or reason not in {"Control taken", "Service stopped"} else self.fault
        for controller in self.controllers.values():
            controller.release()
        current = asyncio.current_task()
        if self.motion and self.motion is not current and not self.motion.done():
            self.motion.cancel()
            try:
                await self.motion
            except asyncio.CancelledError:
                pass

    def reset(self):
        if self.arm.moving:
            raise ValueError("Wait until the arm has stopped")
        self.estopped = False
        self.fault = None
        self.arm.fault = None

    async def power_on(self, go_home):
        await self.connect()
        if not self.arm.connected:
            raise ValueError(self.fault or "The arm is not connected")
        if self.estopped:
            raise ValueError("Reset the stop first")
        if not self.arm.zeroed:
            await asyncio.to_thread(self.arm.confirm_zero, True)
        if not self.arm.torque:
            await asyncio.to_thread(self.arm.enable)
        self.fault = None
        if go_home:
            await self.home()

    async def release_torque(self):
        for controller in self.controllers.values():
            await controller.stop("Torque released")
        await self.stop("Torque released")
        await asyncio.to_thread(self.arm.release)

    def planning_start(self):
        start = np.asarray(self.arm.q, dtype=float).copy()
        excess = np.maximum(self.kin.lower - start, start - self.kin.upper)
        if np.any(excess > math.radians(5)):
            joint = int(np.argmax(excess))
            raise ValueError(f"Joint {joint + 1} is {math.degrees(excess[joint]):.1f} degrees past its limit; move it back by hand with the motors off")
        return np.clip(start, self.kin.lower, self.kin.upper)

    async def home(self):
        self.require_ready()
        await self.stop("Homing")
        self.fault = None
        generation = self.generation
        samples = self.kin.trajectory(self.planning_start(), np.zeros(6), HOME_SPEED, HOME_ACCELERATION)

        async def run():
            self.arm.enabled = True
            previous = 0.0
            for at, q in samples[1:]:
                await asyncio.sleep(at - previous)
                previous = at
                if generation != self.generation:
                    return
                self.require_ready()
                self.arm.write(q, 0.02)
            deadline = time.monotonic() + 10
            while self.arm.moving and time.monotonic() < deadline and generation == self.generation:
                await asyncio.sleep(0.02)
            self.arm.stop()

        self.motion = asyncio.create_task(run())
        try:
            await self.motion
        except ValueError as error:
            self.arm.stop()
            self.fault = str(error)
            raise

    async def home_with(self, controller, principal):
        resume = controller.active
        if resume:
            await controller.stop("Homing", keep_source=True)
        try:
            await self.home()
        finally:
            if resume and not self.estopped and self.arm.torque and not self.arm.fault:
                try:
                    await controller.start(principal)
                except ValueError as error:
                    self.fault = str(error)

    def save_gripper(self, which):
        if which not in {"open", "closed"}:
            raise ValueError("Save the gripper as open or closed")
        saved = self.settings.get("gripper_range") or {"closed": math.degrees(self.arm.gripper_closed), "open": math.degrees(self.arm.gripper_open)}
        saved[which] = math.degrees(float(self.arm.measured[6]))
        self.arm.set_gripper_range(math.radians(saved["closed"]), math.radians(saved["open"]))
        self.settings.set("gripper_range", saved)
        return saved

    def status(self):
        arm = self.arm.status()
        return {
            **arm, "owner": self.owner, "estopped": self.estopped, "fault": self.fault or arm["fault"], "homing": bool(self.motion and not self.motion.done()),
            "joints_deg": [round(math.degrees(v), 1) for v in self.arm.q], "remote_seconds": max(0, round(self.remote_until - time.monotonic())),
            "controllers": {name: controller.status() for name, controller in self.controllers.items()},
        }
