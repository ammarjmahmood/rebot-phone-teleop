import asyncio
import ipaddress
import json
import math
import time
from contextlib import asynccontextmanager
import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from rebot_teleop import config as C
from rebot_teleop import links, twin
from rebot_teleop.recorder import Recorder
from rebot_teleop.motorlog import MotorLog, report_text
from rebot_teleop.usb_link import QuestUsb
from rebot_teleop.cameras import Cameras
from rebot_teleop.kin import top_down
from rebot_teleop.lift import Lift
from rebot_teleop.robot import Robot
from rebot_teleop.sequencer import Sequencer
from rebot_teleop.teleop import Teleop

TRUSTED = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "::1/128", "fd00::/8", "fe80::/10")]


def trusted(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in ("testclient", "localhost")
    return any(address in network for network in TRUSTED)


def rotation_from(value):
    if value is None:
        return None
    if isinstance(value, str):
        if value == "down":
            return top_down(0.0)
        raise ValueError("rotation is 'down', a yaw in degrees, or a 3x3 matrix")
    if isinstance(value, (int, float)):
        return top_down(float(value))
    m = np.asarray(value, dtype=float)
    if m.shape != (3, 3):
        raise ValueError("rotation must be 3x3")
    return m


def optical_pose(position, rotation, offset=0.021):
    forward = rotation[:, 2]
    down = rotation[:, 0]
    right = np.cross(down, forward)
    pose = np.eye(4)
    pose[:3, :3] = np.column_stack([right, down, forward])
    pose[:3, 3] = position + forward * offset
    return pose


def look_at_pose(xyz, target):
    position = np.asarray(xyz, dtype=float)
    forward = np.asarray(target, dtype=float) - position
    forward = forward / np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right = right / (np.linalg.norm(right) or 1.0)
    down = np.cross(forward, right)
    pose = np.eye(4)
    pose[:3, :3] = np.column_stack([right, down, forward])
    pose[:3, 3] = position
    return pose


def create_app(sim=False, arms=("left", "right"), start_hardware=True):
    robot = Robot(arms, sim=sim)
    lift = Lift()
    cameras = Cameras(sim=sim)
    teleop = Teleop(robot, lift)
    sequencer = Sequencer(robot, lift)
    gist = links.Gist()
    usb = QuestUsb()
    recorder = Recorder(robot, cameras, teleop)
    motorlog = MotorLog(robot, mode=lambda: "teleop" if teleop.active else "sequence" if sequencer.playing else "idle")
    cap_runs = {"current": None, "events": [], "seq": 0}
    scene_cache = {}

    @asynccontextmanager
    async def lifespan(app):
        if start_hardware:
            robot.start()
            motorlog.start()
            cameras.start()
            if not sim:
                lift.start()
                gist.run_forever()
                usb.start()
        yield
        recorder.stop()
        motorlog.stop()
        teleop.stop()
        robot.close()
        cameras.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.robot, app.state.teleop, app.state.sequencer, app.state.cameras, app.state.lift = robot, teleop, sequencer, cameras, lift
    app.mount("/static", StaticFiles(directory=C.STATIC), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.client and not trusted(request.client.host):
            return JSONResponse({"detail": "Only the local network and the tailnet may use the robot"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path in ("/", "/quest", "/phone"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def refused(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    async def body(request):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def full_status():
        return {
            **robot.status(), "teleop": teleop.status(), "record": recorder.status(), "sequencer": sequencer.status(), "lift": lift.status(),
            "cameras": cameras.status(), "links": links.urls(), "gist": {"url": gist.url, "status": gist.status}, "usb": usb.status(), "time": time.time(),
        }

    @app.get("/")
    async def index():
        return FileResponse(C.STATIC / "index.html")

    @app.get("/quest")
    async def quest():
        return FileResponse(C.STATIC / "quest.html")

    @app.get("/phone")
    async def phone():
        return FileResponse(C.STATIC / "phone.html")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        return full_status()

    @app.get("/api/scene")
    async def scene():
        if "scene" not in scene_cache:
            scene_cache["scene"] = await asyncio.to_thread(twin.scene)
        return scene_cache["scene"]

    @app.get("/api/mesh/{key}")
    async def mesh(key: str):
        data = twin.mesh_bytes(key.replace("/", "").replace(".", ""))
        if data is None:
            return Response(status_code=404)
        return Response(data, media_type="model/stl", headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/power")
    async def power(request: Request):
        data = await body(request)
        if data.get("on", True):
            return await asyncio.to_thread(robot.power_on, data.get("arm"))
        print(f"Torque off for {data.get('arm') or 'both arms'} requested by {request.client.host if request.client else 'unknown'}", flush=True)
        teleop.release_all()
        return await asyncio.to_thread(robot.power_off, data.get("arm"))

    @app.post("/api/stop")
    async def stop():
        sequencer.stop_flag.set()
        robot.stop()
        lift.stop()
        return {"stopped": True}

    @app.post("/api/hold")
    async def hold():
        sequencer.stop_flag.set()
        robot.stop()
        lift.stop()
        try:
            result = await asyncio.to_thread(robot.power_on, None)
        except ValueError as error:
            result = {"powered": [], "notes": [str(error)]}
        return {"holding": result["powered"], "notes": result["notes"]}

    @app.post("/api/clear")
    async def clear():
        robot.clear_faults()
        return {"cleared": True}

    async def wait_done(data):
        if data.get("wait", True):
            await asyncio.to_thread(robot.wait, float(data.get("timeout", 60)))

    @app.post("/api/move")
    async def move(request: Request):
        data = await body(request)
        speed = float(data.get("speed_deg_s", C.MOVE_DEG_S))
        if data.get("pose"):
            duration = robot.go(data["pose"], data.get("arm"), speed)
            await wait_done(data)
            return {"pose": data["pose"], "duration_s": round(duration, 2), "state": robot.status()["arms"]}
        unit = robot.unit(data.get("arm"))
        if not unit.ready:
            robot.ensure_powered(unit.name)
        if not unit.ready:
            raise ValueError(unit.arm.status()["note"] or f"The {unit.name} arm is not powered")
        result = {}
        if data.get("joints_deg") is not None:
            joints = np.radians(np.asarray(data["joints_deg"], dtype=float))
            if joints.shape != (6,):
                raise ValueError("joints_deg needs six numbers")
            unit.check_table(joints)
            result["duration_s"] = unit.arm.move(joints, speed)
            result["load"] = unit.load_report(joints)
        elif data.get("tip_xyz") is not None or data.get("tip_delta") is not None:
            current, rotation = unit.kin.pose(unit.arm.command[:6])
            goal = np.asarray(data["tip_xyz"], dtype=float) if data.get("tip_xyz") is not None else current + np.asarray(data["tip_delta"], dtype=float)
            q, err, duration = unit.move_tip(goal, rotation_from(data.get("rotation")) if data.get("rotation") is not None else rotation, speed)
            result.update({"duration_s": duration, "reach_error_m": round(err, 4), "joints_deg": [round(math.degrees(v), 2) for v in q]})
            if err > 0.01:
                result["note"] = f"Out of reach by {err * 100:.1f} cm; moved as close as the arm can get"
            result["load"] = unit.load_report(q)
        if result.get("load"):
            worst = result["load"]["worst"]
            if abs(worst["nm"]) > worst["hold_nm"]:
                left = worst["seconds_before_relax"]
                result["load_note"] = f"Joint {worst['joint']} holds about {abs(worst['nm']):.0f} Nm here, more than the {worst['hold_nm']:.0f} Nm it holds for long" + (f"; about {left} s before the arm relaxes itself. Keep this short, or bring the hand closer in or lower" if left is not None else "")
        if data.get("gripper_m") is not None:
            unit.arm.grip(float(data["gripper_m"]))
        await wait_done(data)
        if data.get("gripper_m") is not None and data.get("wait", True):
            await asyncio.sleep(0.6)
        result["state"] = unit.status()
        return result

    @app.post("/api/reach")
    async def reach(request: Request):
        data = await body(request)
        unit = robot.unit(data.get("arm"))
        current, rotation = unit.kin.pose(unit.arm.command[:6] if unit.arm.torque else unit.arm.q)
        rot = rotation_from(data.get("rotation")) if data.get("rotation") is not None else rotation
        q, err, rerr = unit.kin.ik(np.asarray(data["tip_xyz"], dtype=float), rot, unit.arm.q, iterations=80)
        return {"reachable": err < 0.01, "position_error_m": round(err, 4), "rotation_error_deg": round(math.degrees(rerr), 1), "joints_deg": [round(math.degrees(v), 2) for v in q]}

    @app.post("/api/jog")
    async def jog(request: Request):
        data = await body(request)
        unit = robot.unit(data.get("arm"))
        if not unit.ready:
            raise ValueError(f"Power the {unit.name} arm on first")
        joint = int(data.get("joint", 0))
        q = unit.arm.target[:6].copy()
        q[joint] += math.radians(float(data.get("delta_deg", 5.0)))
        unit.arm.move(q, 60.0)
        return {"ok": True}

    @app.post("/api/gripper")
    async def gripper(request: Request):
        data = await body(request)
        arms = robot.pick(data.get("arm"))
        for unit in arms:
            if data.get("calibrate") in ("open", "closed"):
                unit.arm.gripper_is(data["calibrate"])
                continue
            width = data.get("width_m")
            if width is None:
                width = C.GRIPPER_WIDTH_M if data.get("state", "open") == "open" else 0.0
            unit.arm.grip(float(width))
        return {"ok": True}

    @app.post("/api/pose/save")
    async def pose_save(request: Request):
        data = await body(request)
        return robot.save_pose(str(data.get("name") or "rest"), data.get("arm"))

    @app.get("/api/poses")
    async def poses():
        return {"builtin": C.POSES_DEG, "saved": C.load("poses.json", {})}

    @app.post("/api/lift")
    async def lift_command(request: Request):
        data = await body(request)
        if data.get("speed") is not None:
            return lift.set_speed(data["speed"])
        if data.get("hold"):
            lift.hold(data.get("action", "up"))
            return lift.status()
        return await asyncio.to_thread(lift.command, data.get("action", "stop"))

    def camera_jpeg(name):
        camera = cameras.get(name)
        return camera.jpeg_bytes()

    @app.get("/api/camera/{name}.jpg")
    async def camera_still(name: str):
        jpeg, _ = camera_jpeg(name)
        for _ in range(30):
            if jpeg:
                break
            await asyncio.sleep(0.05)
            jpeg, _ = camera_jpeg(name)
        if not jpeg:
            return Response(status_code=503)
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/camera/{name}.mjpg")
    async def camera_stream(name: str, request: Request):
        cameras.get(name)

        async def frames():
            last = -1
            while not await request.is_disconnected():
                jpeg, count = camera_jpeg(name)
                if jpeg and count != last:
                    last = count
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(0.01)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})

    def camera_geometry(name):
        camera = cameras.get(name)
        image, depth, _ = camera.frame()
        if image is None:
            raise ValueError(f"No picture from the {name} camera yet: {camera.status()['note']}")
        height, width = image.shape[:2]
        intrinsics = getattr(camera, "intrinsics", None)
        if not intrinsics:
            f = (width / 2) / math.tan(math.radians(cameras.fov.get(name, 70.0)) / 2)
            intrinsics = {"fx": f, "fy": f, "cx": width / 2, "cy": height / 2}
        if name in robot.units:
            unit = robot.units[name]
            position, rotation = unit.kin.camera_pose(unit.arm.q)
            pose = optical_pose(position, rotation)
        else:
            pose = look_at_pose(cameras.head_pose["xyz"], cameras.head_pose["look_at"])
        return image, depth, {"width": width, "height": height, **intrinsics}, pose

    @app.post("/api/camera/{name}/locate")
    async def camera_locate(name: str, request: Request):
        data = await body(request)
        image, depth, info, pose = camera_geometry(name)
        plane = data.get("plane_z")
        out = []
        for u, v in data.get("pixels", []):
            ray = np.array([(float(u) - info["cx"]) / info["fx"], (float(v) - info["cy"]) / info["fy"], 1.0])
            if depth is not None and plane is None:
                d = depth[min(max(int(v), 0), depth.shape[0] - 1), min(max(int(u), 0), depth.shape[1] - 1)] * 0.001
                if d > 0:
                    out.append({"xyz": np.round(pose[:3, :3] @ (ray * d) + pose[:3, 3], 4).tolist(), "method": "depth", "distance_m": round(float(d), 4)})
                    continue
            z = float(plane if plane is not None else robot.table["top"])
            direction = pose[:3, :3] @ ray
            t = (z - pose[2, 3]) / direction[2] if abs(direction[2]) > 1e-6 else -1
            out.append({"xyz": np.round(pose[:3, 3] + direction * t, 4).tolist() if t > 0 else None, "method": f"plane z={z}"})
        return {"points": out, "camera_pose": np.round(pose, 4).tolist(), "info": info}

    @app.get("/api/camera/{name}/info")
    async def camera_info(name: str):
        _, depth, info, pose = camera_geometry(name)
        return {"info": info, "camera_pose": np.round(pose, 4).tolist(), "depth": depth is not None}

    @app.post("/api/teleop/start")
    async def teleop_start(request: Request):
        data = await body(request)
        return await asyncio.to_thread(teleop.start, data.get("source", "dashboard"))

    @app.get("/api/torque")
    async def torque():
        return {"now": time.time(), "seconds": C.TORQUE_HISTORY_S, "arms": {name: unit.arm.torque_history() for name, unit in robot.units.items()}}

    @app.get("/api/table")
    async def table_get():
        return robot.table

    @app.post("/api/table")
    async def table_set(request: Request):
        data = await body(request)
        return robot.set_table(data.get("top"), data.get("front"))

    @app.get("/api/motorlog/report")
    async def motorlog_report(hours: float = 24.0, arm: str = None, format: str = "json"):
        data = await asyncio.to_thread(motorlog.report, hours, arm or None)
        if format == "text":
            return PlainTextResponse(report_text(data))
        return data

    @app.post("/api/record/start")
    async def record_start():
        return await asyncio.to_thread(recorder.start)

    @app.post("/api/record/stop")
    async def record_stop():
        return await asyncio.to_thread(recorder.stop)

    @app.post("/api/teleop/stop")
    async def teleop_stop():
        return teleop.stop()

    @app.post("/api/teleop/settings")
    async def teleop_settings(request: Request):
        return teleop.configure(**(await body(request)))

    @app.get("/api/seq")
    async def seq_list():
        return {"sequences": sequencer.list(), **sequencer.status()}

    @app.get("/api/seq/{name}")
    async def seq_get(name: str):
        return sequencer.get(name)

    @app.put("/api/seq/{name}")
    async def seq_put(name: str, request: Request):
        data = await body(request)
        data["name"] = data.get("name") or name
        saved = sequencer.save(data)
        if saved["name"] != name:
            sequencer.delete(name)
        return saved

    @app.delete("/api/seq/{name}")
    async def seq_delete(name: str):
        return sequencer.delete(name)

    @app.post("/api/seq/{name}/capture")
    async def seq_capture(name: str, request: Request):
        data = await body(request)
        step = sequencer.capture(data.get("arm"), float(data.get("speed_deg_s", C.MOVE_DEG_S)), float(data.get("pause_s", 0)))
        return sequencer.add(name, step, data.get("index"))

    @app.post("/api/seq/{name}/step")
    async def seq_step(name: str, request: Request):
        data = await body(request)
        step = data.get("step") or {}
        if step.get("type") not in ("wait", "gripper", "lift", "pose"):
            raise ValueError("Add a wait, gripper, lift or pose step")
        return sequencer.add(name, step, data.get("index"))

    @app.post("/api/seq/record/start")
    async def seq_record_start(request: Request):
        data = await body(request)
        return sequencer.start_recording(data.get("arm"), float(data.get("rate_hz", 50)))

    @app.post("/api/seq/record/stop")
    async def seq_record_stop(request: Request):
        data = await body(request)
        step = sequencer.stop_recording(data.get("name"), data.get("label"))
        return {"steps": len(step["t"]), "seconds": round(step["t"][-1] - step["t"][0], 2)}

    @app.get("/api/seq/{name}/plan")
    async def seq_plan(name: str, speed: float = None):
        return await asyncio.to_thread(sequencer.plan, sequencer.get(name), speed)

    @app.post("/api/seq/{name}/play")
    async def seq_play(name: str, request: Request):
        data = await body(request)
        return await asyncio.to_thread(sequencer.play, name, None, data.get("loop"), data.get("speed"), data.get("start", 0))

    @app.post("/api/seq/stop")
    async def seq_stop():
        return sequencer.stop()

    @app.get("/api/qr.svg")
    async def qr(text: str):
        import io
        import segno
        out = io.BytesIO()
        segno.make(text, error="m").save(out, kind="svg", scale=4, border=2, dark="#111", light="#fff")
        return Response(out.getvalue(), media_type="image/svg+xml")

    @app.get("/api/links")
    async def links_list():
        return {"links": links.urls(), "gist": {"url": gist.url, "status": gist.status}}

    @app.post("/api/cap")
    async def cap_start(request: Request):
        from rebot_teleop.cap import CapRun
        data = await body(request)
        instruction = str(data.get("instruction", "")).strip()
        if not instruction:
            raise ValueError("Type what the robot should do")
        current = cap_runs["current"]
        if current is not None and current.alive:
            raise ValueError("A policy is already running; stop it first")
        cap_runs["events"] = []

        def emit(kind, text, **extra):
            cap_runs["seq"] += 1
            cap_runs["events"].append({"id": cap_runs["seq"], "kind": kind, "text": text, "at": time.time(), **extra})
            del cap_runs["events"][:-400]

        run = CapRun(f"http://127.0.0.1:{C.HTTP_PORT}", instruction, model=data.get("model") or None, effort=data.get("effort") or "medium", emit=emit)
        cap_runs["current"] = run
        run.start()
        return {"started": True}

    @app.get("/api/cap")
    async def cap_status(after: int = 0):
        current = cap_runs["current"]
        return {"running": bool(current and current.alive), "events": [e for e in cap_runs["events"] if e["id"] > after]}

    @app.post("/api/cap/stop")
    async def cap_stop():
        current = cap_runs["current"]
        if current:
            current.stop()
        robot.stop()
        return {"stopped": True}

    @app.post("/api/policy/run")
    async def policy_run(request: Request):
        from rebot_teleop.agent import run_code
        data = await body(request)
        return await asyncio.to_thread(run_code, str(data.get("code", "")), float(data.get("timeout_s", 120)), f"http://127.0.0.1:{C.HTTP_PORT}")

    def socket_trusted(websocket):
        return websocket.client is None or trusted(websocket.client.host)

    @app.websocket("/ws/state")
    async def state_socket(websocket: WebSocket):
        if not socket_trusted(websocket):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        tick = 0
        try:
            while True:
                message = {"twin": robot.twin(), "arms": {n: u.status() for n, u in robot.units.items()}, "teleop": teleop.status(), "record": recorder.status(), "sequencer": sequencer.status(), "lift": lift.status(), "sim": robot.sim}
                if tick % 10 == 0:
                    message.update({"cameras": cameras.status(), "links": links.urls(), "usb": usb.status(), "gist": {"url": gist.url, "status": gist.status}, "can": robot.status()["can"]})
                await websocket.send_text(json.dumps(message))
                tick += 1
                await asyncio.sleep(0.05)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.websocket("/ws/teleop")
    async def teleop_socket(websocket: WebSocket):
        if not socket_trusted(websocket):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        last_report = 0.0
        try:
            while True:
                message = json.loads(await websocket.receive_text())
                kind = message.get("type", "hands")
                if kind == "hands":
                    sent = teleop.push_hands(message, message.get("source", "quest"))
                elif kind == "velocity":
                    teleop.push_velocity(message.get("arm", "left"), message.get("linear", [0, 0, 0]), message.get("angular", [0, 0, 0]), message.get("grip"), message.get("clutch", True))
                    sent = message.get("t")
                elif kind == "start":
                    result = await asyncio.to_thread(teleop.start, message.get("source", "quest"))
                    await websocket.send_text(json.dumps({"type": "started", **result}))
                    continue
                elif kind == "stop":
                    teleop.stop()
                    continue
                elif kind == "settings":
                    teleop.configure(**message.get("settings", {}))
                    continue
                elif kind == "lift":
                    if message.get("action") in ("up", "down"):
                        lift.hold(message["action"])
                    else:
                        lift.release()
                    continue
                elif kind == "power":
                    await asyncio.to_thread(robot.ensure_powered)
                    continue
                else:
                    continue
                now = time.monotonic()
                if now - last_report > 0.2:
                    last_report = now
                    report = {"type": "status", "t": sent, "teleop": teleop.status(), "arms": {n: {"ready": u.ready, "note": u.status()["note"], "fault": u.arm.fault, "connected": u.arm.connected, "heat": u.arm.heat.summary() if u.arm.torque else None, "rest": u.arm.rest_status()} for n, u in robot.units.items()}}
                    await websocket.send_text(json.dumps(report))
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            teleop.release_all()
            lift.release()

    @app.websocket("/ws/app")
    async def app_socket(websocket: WebSocket):
        if not socket_trusted(websocket):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        chosen = {"arm": next(iter(robot.units))}

        async def report():
            while True:
                unit = robot.units[chosen["arm"]]
                await websocket.send_json({"type": "status", "owner": True, "active": teleop.active, "mode": "motion" if teleop.hands[chosen["arm"]].engaged else None, "note": unit.status()["note"], "calibrated": True, "tilt": teleop.settings.get("rotation", True), "view": teleop.settings.get("view"), "torque": unit.arm.torque, "fault": unit.arm.fault, "remote": True, "joints": [round(math.degrees(v), 1) for v in unit.arm.q], "gripper": round(math.degrees(unit.arm.measured[6]), 1), "phase": "TELEOP" if teleop.active else "READY", "arm": chosen["arm"], "arms": list(robot.units)})
                await asyncio.sleep(0.1)

        reporter = asyncio.create_task(report())
        try:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")
                if kind == "arm" and message.get("arm") in robot.units:
                    chosen["arm"] = message["arm"]
                elif kind == "pose":
                    w, x, y, z = (float(v) for v in message["q"][:4])
                    grip = message.get("grip")
                    teleop.push_hands({"yaw": 0.0, "hands": {chosen["arm"]: {"p": [float(v) for v in message["p"][:3]], "q": [x, y, z, w], "grip": 1.0 if message.get("move") else 0.0, "trigger": 0.0 if grip is None else 1.0 - float(grip)}}}, "phone app")
                elif kind == "start":
                    await asyncio.to_thread(teleop.start, "phone app")
                elif kind == "stop":
                    teleop.stop()
                elif kind == "home":
                    robot.go("home", chosen["arm"])
        except (WebSocketDisconnect, RuntimeError, ValueError, KeyError, TypeError):
            pass
        finally:
            reporter.cancel()
            teleop.release_all()

    return app
