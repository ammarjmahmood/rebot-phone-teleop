# reBot

Everything for the two armed reBot (two Seeed B601 RS arms on a post) in plain Python on the Jetson Orin. No ROS.

* **Dashboard** at `http://<jetson>:8081/`, full screen on the Jetson's own display at boot:
  * **Home**: live 3D twin that follows the real joints (also when an arm is moved by hand with torque off), all three cameras, power, poses, per joint angle, temperature, torque and gravity load, gripper, table lift.
  * **Teleop**: Quest VR and phone links with QR codes, live camera, teleop settings and status.
  * **Sequencer**: save points or record real motion, reorder by dragging, mix in waits, gripper and table steps, play, loop, change speed.
  * **Policy**: type an instruction and watch Claude write and run robot code, or run Python yourself.
* **Quest VR** at `https://<jetson>:8443/quest`: squeeze grip to move that arm, trigger closes the gripper. Works with one arm or both. The link is also kept on the GitHub gist bookmark.
* **Phone** at `https://<jetson>:8443/phone`: touch sticks drive the gripper tip.
* **Motor budget**: every joint tracks how much of its RobStride overload allowance it has used. The Home tab shows it per joint, the Teleop tab and the Quest headset show the time left, and an arm that runs out relaxes at home, recovers and goes back up by itself. The Sequencer rests the arms before steps that would run out. See `docs/ROBOT_GUIDE.md`.
* **Joint 1 counterbalance**: printable drum, pulley and spring parts that carry part of the shoulder load, with a shopping list and a per arm setting so the software knows. See `hardware/counterbalance/README.md`.
* **Teleop recording**: the Record teleop button on the Teleop tab saves a folder in `~/.rebot-teleop/recordings/`: `states.jsonl` (both arms at 50 Hz), one `.mjpg` and `.times.txt` per camera, and `meta.json`. It is not a rosbag, since there is no ROS here.
* **Claude Code**: the `rebot` MCP server is registered for every `claude` session on the Jetson (code as policy).

## Run

```bash
.venv/bin/python -m rebot_teleop            # real arms
.venv/bin/python -m rebot_teleop --sim      # simulated arms, no CAN traffic
.venv/bin/python -m pytest -q tests         # tests on the simulated bus
```

On the Jetson it runs as the `rebot` systemd service. Run `scripts/install_jetson.sh` once (it asks for sudo) to install the service, CAN and camera udev rules, auto login with the full screen dashboard, and to remove ROS.

## Hardware

| Part | Connection | Code |
|---|---|---|
| Left and right arm, RobStride RS06 (J1 to J3) and RS00 (J4 to J6, gripper) | PEAK USB CAN at 1 Mbit/s, tied to USB ports in `~/.rebot-teleop/can_ports.json` | `rebot_teleop/arm.py` (motorbridge, MIT mode at 200 Hz) |
| Wrist cameras, Innomaker U20CAM | USB, `/dev/v4l/by-id/...` | `rebot_teleop/cameras.py` (OpenCV V4L2, MJPEG passthrough) |
| Head camera, Angstrong HP60C (RGB and depth) | USB | `rebot_teleop/hp60c.py` (vendor SDK through ctypes, in `vendor/angstrong`) |
| Table lift, Pico with BTS7960 | USB serial | `rebot_teleop/lift.py` |

Joint limits are Seeed's URDF limits less 2 degrees (`config.py`). Gravity compensation comes from the URDF model with the side mounts. On power on each arm holds exactly where it is. An arm that is unplugged or unpowered is shown offline and everything else keeps working; it is picked up again within two seconds of being plugged back in.

## Files

```
rebot_teleop/
  config.py      limits, gains, mounts, poses, device paths
  arm.py         RobStride arm driver: connect, whole turn correction, servo and trajectory loop, gripper
  simbus.py      simulated CAN bus for --sim and tests
  kin.py         Pinocchio kinematics and inverse kinematics
  robot.py       both arms, hot plug, poses
  teleop.py      Quest and phone teleop (telegrip style relative control at 125 Hz)
  sequencer.py   points, recorded motion, playback
  cameras.py     USB cameras;  hp60c.py  head depth camera
  lift.py        table lift
  twin.py        meshes for the dashboard twin
  server.py      FastAPI: pages, REST, WebSockets, MJPEG
  agent.py       the robot object for policies;  mcp_server.py  MCP tools;  cap.py  Claude CLI runner
  kiosk.py       full screen dashboard window on the Jetson display
```

Everything removed in the cleanup of 2026-11-10 (ROS bridge, PyBullet simulator and task benchmark, HEBI, pairing codes) is on the branch `archive/pre-cleanup-2026-11-10`.
