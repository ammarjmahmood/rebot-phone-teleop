# reBot Teleop

Teleoperate the [Seeed Studio reBot Arm B601 RS](https://wiki.seeedstudio.com/rebot_b601_rs_getting_started/) with an iPhone, the HEBI Mobile I/O app or a Meta Quest. Move the phone and the gripper follows.

The phone interaction follows the approach published with **TidyBot++** (Wu et al., CoRL 2024), adapted to a six axis desktop arm, a native ARKit app and RobStride motors over CAN. As far as we could find, this is the first phone teleoperation for the reBot and the first open package combining phone, HEBI and Quest control for the B601 RS. Related reBot work includes Seeed's reBot Arm 102 leader arm, a community VR demo and a community ROS2 gamepad IK teleop.

## Status

The iPhone path was developed and used on a real B601 RS with an iPhone 16 Pro Max. This standalone package was refactored out of that working code and is covered by tests against a simulated motor bus. The HEBI and Quest paths are newer and less tested on hardware. Issues and pull requests are welcome.

## What you get

1. **iPhone app** (`ios/RebotTeleop`, SwiftUI and ARKit). Hold the phone upright, put your thumb on the pad and move: the gripper copies your hand's translation and rotation. A separate strip opens and closes the gripper without moving the arm, or you can slide your thumb on the pad to do it while moving.
2. **HEBI Mobile I/O** support for any iPhone or iPad without building an app. Hold Move (B1), B2 opens, B3 closes, B8 homes. A one time three move calibration learns the phone's axes from motion, so it works however HEBI reports them.
3. **Meta Quest** through the Quest browser (WebXR, passthrough). Hold the right grip to move, trigger closes the gripper, B homes.
4. **Arm server** for the computer on the CAN bus: a 200 Hz MIT streaming driver with acceleration limited motion, gravity feedforward, Seeed's joint limits, gripper stall protection, damped least squares IK on Seeed's official RS model, and a small control page to power on, home, pair devices, allow motion and stop.

## Requirements

1. reBot Arm B601 RS with motors at IDs 1 to 7 and zero positions written (MotorBridge Studio, see Seeed's quick start).
2. A PCAN USB adapter on Linux, set to 1 Mbit/s:
   ```sh
   sudo ip link set can0 down
   sudo ip link set can0 type can bitrate 1000000 restart-ms 100
   sudo ip link set can0 up
   ```
   Close MotorBridge Studio and its gateway before starting; only one program can drive the bus.
3. Python 3.11 and [uv](https://docs.astral.sh/uv/).
4. For the iPhone app: a Mac with Xcode 15 or newer and an Apple developer account. For HEBI: the free HEBI Mobile I/O app. For VR: a Quest 3 or Quest Pro.

## Install and run

On the Linux computer connected to the arm (PC, Jetson or Raspberry Pi), one command installs everything: uv, Python 3.11, this repository in `~/rebot-phone-teleop` and the PEAK CAN driver and settings. It asks for your password once for the CAN setup.

```sh
curl -fsSL https://raw.githubusercontent.com/ammarjmahmood/rebot-phone-teleop/main/install.sh | bash
```

Add HEBI Mobile I/O support with `| bash -s -- --hebi`, or skip the CAN setup with `| bash -s -- --no-can`. Running it again updates an existing install.

Start the server:

```sh
cd ~/rebot-phone-teleop && .venv/bin/python -m rebot_teleop --lan
```

It finds the PEAK adapter automatically (on a Jetson the built in CAN controller already takes `can0`); pass `--can can1` to choose one. Open http://127.0.0.1:8080 on that computer.

1. Press **Power on and go to zero**. The arm can start in any pose inside its joint ranges; it enables holding position and drives slowly to zero. **Power on and hold here** enables without moving.
2. Tick **Allow phone and headset motion** (lasts 15 minutes) and press **Show pairing code**. The page lists this computer's addresses, including a Tailscale address if it has one.

### Reaching it from anywhere

1. **Tailscale**: put the arm computer and the phone on the same tailnet and use `http://<tailscale ip>:8080` in the app. The app allows plain HTTP for this because Tailscale encrypts the traffic itself.
2. **Cloudflare quick tunnel**: run `scripts/setup_cloudflared.sh` once, then start the server with `--cloudflare`. Show pairing code then lists an `https://….trycloudflare.com` address that works over mobile data with no VPN app. It is a public address, so pairing and the motion permission at the arm computer are what protect it, and it adds some latency compared with Tailscale. The address changes each time the server starts.
3. Other names, such as your own domain behind a reverse proxy, can be allowed with `--allow-host arm.example.com` or `--allow-host "*.example.com"`.

### Two arms

Give each arm its own PEAK adapter and name them when starting:

```sh
.venv/bin/python -m rebot_teleop --lan --arm left=can1 --arm right=can2
```

`--list-can` shows each CAN interface with its USB port so you can tell the adapters apart; interface numbers follow plug order, so keep each adapter in the same USB port. Every arm gets its own card on the control page (power, home, release, gripper range), its own safety monitor and its own settings. In the iPhone app a Left / Right switch picks the arm you drive; switching hands control over instantly. On the Quest the left controller drives the left arm and the right controller the right arm. Stop stops both. The arms do not know about each other, so mount them far enough apart that they cannot collide.

### Quest bookmark with a GitHub gist

With `--cloudflare`, the server can write the current tunnel address into a secret GitHub gist each time it starts, so the Quest only needs one bookmark. Create a GitHub token that can only manage gists (Settings, Developer settings, fine grained token or classic token with the `gist` scope) and add it to `~/.rebot-teleop/.env` on the arm computer:

```
REBOT_TELEOP_GIST_TOKEN="<token>"
```

On the first start the server creates the gist and shows its link on the control page under Phone and headset access; later starts update the same gist. Open that gist on the Quest and tap the Quest link.

### Running on a headless Jetson or another remote computer

Power on, pairing and allowing motion are only accepted from the arm computer itself. From your laptop, open an SSH tunnel so the page counts as local:

```sh
ssh -L 8080:localhost:8080 <user>@<jetson address>
```

then open http://127.0.0.1:8080 on the laptop. Phones and the Quest connect to the Jetson's own WiFi or Tailscale address as usual.

On Jetson the kernel ships without the PEAK USB driver; `scripts/setup_can.sh` (run by the installer) builds it from the matching Linux source against the JetPack kernel headers and loads it. Tested on a Jetson Orin with JetPack 6 (L4T R36.4, kernel 5.15.148).

### iPhone app

Build it on a Mac (details and an agent brief in `ios/AGENTS.md`):

```sh
brew install xcodegen
cd ios/RebotTeleop && xcodegen && open RebotTeleop.xcodeproj
```

Choose your team under Signing and Capabilities and run it on the phone. On the control page press Show pairing code and scan the QR code for the route you want (WiFi, Tailscale or Cloudflare) with the iPhone camera; the app opens with the address and code filled in and pairs. You can also type the address and code by hand. Hold the phone upright with the camera looking the way you face, choose where you stand relative to the arm, then put your thumb on the pad and move.

### HEBI Mobile I/O

```sh
scripts/setup_hebi.sh
```

Open HEBI Mobile I/O on the phone on the same network, press **Start HEBI** on the control page, then **Calibrate up, forward and left**: for each, hold Move and move the phone about 10 cm in that direction. Away from the local network, put the phone's Tailscale address in the HEBI section; discovery is then sent directly to that address.

### Meta Quest

On the control page of the arm computer:

1. Press **Power on and go to zero**.
2. Tick **Allow phone and headset motion for 15 minutes**.
3. Press **Show pairing code** and note the eight digit code (single use, five minutes).

On the Quest:

1. Open the Quest browser at the Quest address listed under Show pairing code. With `--cloudflare` this is `https://<name>.trycloudflare.com/quest`, which has a valid certificate and works from anywhere. On the same network you can use `https://<computer>:9443/quest` and accept the local certificate warning once (Advanced, Proceed).
2. Enter the pairing code and choose where you stand relative to the arm. Behind the arm, facing the same way, is the most intuitive.
3. Face that way and press **Enter VR**, allowing passthrough so you see the real arm.

Controls: hold a **grip** button and that arm copies your hand's movement and rotation from the moment you squeezed; release to stop and the arm holds. The **trigger** closes that arm's gripper. **B** homes the right arm and **Y** the left. With one arm, use the right controller. Exit VR or press **Stop VR control** to hand control back. Start with small, slow moves and keep the control page's STOP button within reach.

## How it works

For every device the server keeps a reference taken when you start moving: the device pose and the gripper pose at that instant. While you hold the deadman, the gripper target is

```
position = gripper_position_at_press + A · (device_position − device_position_at_press)
rotation = exp(det(A) · A · log(device_rotation · device_rotation_at_pressᵀ)) · gripper_rotation_at_press
```

where `A` maps the device's world frame to the robot base frame. For the iPhone app it is built from the camera's horizontal heading when you start (as in TidyBot++), for WebXR from the session's initial heading, and for HEBI from the three calibration moves. The `det(A)` factor keeps rotations correct if a device reports a mirrored frame. The target is solved with damped least squares IK on the official RS URDF, checked against joint limits, the table and the workspace, and streamed to the motors through acceleration limited setpoints at 200 Hz. Following TidyBot++, the first two samples after a press are skipped because pose updates lag touch events.

## Safety

This is research software for a small arm. Keep the workspace clear and a hand on the power switch.

1. Connecting only reads the motors. Motors are enabled only from the control page on the arm computer.
2. Every device needs pairing, and phone or headset motion must be allowed at the arm computer, for 15 minutes at a time.
3. Motion needs a held deadman. A stale pose stops motion within 0.15 to 0.3 seconds and the controller drops after 3 seconds without data.
4. Stop freezes the arm where it is and latches until reset at the computer. Closing the server keeps the motors holding rather than dropping the arm.
5. A joint lagging its command by more than 15 degrees for 0.4 seconds, missing feedback or a motor above 80 °C faults and holds.
6. These protections are software. Use the arm's power switch as the emergency stop; this project does not provide a certified one.

## Tests

```sh
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest -q
```

The tests run against a simulated RobStride bus.

## Citation

If this is useful, please cite TidyBot++, whose phone teleoperation design this follows:

```bibtex
@inproceedings{wu2024tidybot,
  title = {TidyBot++: An Open-Source Holonomic Mobile Manipulator for Robot Learning},
  author = {Wu, Jimmy and Chong, William and Holmberg, Robert and Prasad, Aaditya and Gao, Yihuai and Khatib, Oussama and Song, Shuran and Rusinkiewicz, Szymon and Bohg, Jeannette},
  booktitle = {Conference on Robot Learning},
  year = {2024}
}
```

Related earlier work on phone teleoperation: Mandlekar et al., *RoboTurk: A Crowdsourcing Platform for Robotic Skill Learning through Imitation*, CoRL 2018.

## Acknowledgements and licences

1. TidyBot++ by Jimmy Wu et al. ([code](https://github.com/jimmyyhwu/tidybot2), MIT): the interaction design and pose mapping. No TidyBot++ code is copied.
2. Seeed Studio [reBot DevArm](https://github.com/Seeed-Projects/reBot-DevArm): `assets/rebot_rs.urdf` is the official RS model, used under the CERN Open Hardware Licence version 2, weakly reciprocal (`licenses/rebot-hardware-CERN-OHL-W.txt`).
3. Seeed Studio [lerobot-robot-seeed-b601](https://github.com/Seeed-Projects/lerobot-robot-seeed-b601) (MIT): motor IDs, MIT gains, gripper travel and torque limits were taken from its RS follower configuration.
4. [MotorBridge](https://github.com/motorbridge/motorbridge) for RobStride CAN access and [Pinocchio](https://github.com/stack-of-tasks/pinocchio) for kinematics and gravity.
5. HEBI Robotics [Mobile I/O](https://docs.hebi.us/tools.html#mobile-io), also used by LeRobot's phone teleoperator.

The code in this repository is released under the MIT licence (`LICENSE`).

## Simulation before hardware

`--sim` runs the same server, IK, gravity compensation, safety checks, dashboard and Quest page against two PyBullet arms on the post assembly. Nothing is sent on CAN.

```bash
python -m rebot_teleop --sim --data ~/.rebot-teleop-sim
python -m rebot_teleop --sim --cloudflare --data ~/.rebot-teleop-sim
```

The first opens a 3D window and the dashboard at http://127.0.0.1:8080. The second also prints a public https link, so a Quest anywhere can drive the simulated arms after pairing with a code from the dashboard. Arms are named `left` and `right` and follow the matching Quest controllers.

Check the whole chain automatically while the server runs:

```bash
python scripts/validate_sim.py --url http://127.0.0.1:8080
```

It powers both arms on, moves virtual Quest hands forward, back, left, right, up and down, rolls and pitches them, squeezes the triggers, then checks that each gripper moved the same way in the world, that nothing hit the post, and that the temperature warning, stop and Reset behave.

### Mounting

`left` and `right` default to the Purpose Robotics post from the CAD in `~/rebot_isaac_sim/robot_head`: eyes face the front, each base plate bolts flat to a side of the 40 x 60 mm leg 155 mm below its top, J1 points out sideways and J1 zero faces the front. Home (also the sim start pose and the Quest B button) is the arms hanging beside the post: left `[-90, 170, 145, 30, 0, 0]`, right `[90, 170, 145, 30, 0, 0]` degrees. Override with `"home_deg"` in that arm's `settings.json`. The same mount is used on hardware, so IK and gravity compensation match the real orientation. Override it per arm with `--mount left=X,Y,Z,ROLL,PITCH,YAW` (metres, degrees), or save `"mount": {"xyz": [...], "rpy_deg": [...]}` in that arm's `settings.json`.

### Hand guiding and the joint check

Hand guiding reads the joints with the motors off, so you can move the real arm by hand and watch the numbers, the model tip position and the twin follow. Press Hand guide on an arm card (it is offered only while torque is off). Power on ends it.

Mirror a running server, real or simulated, in a PyBullet window on any computer:

```bash
python -m rebot_teleop.twin --url http://JETSON_ADDRESS:8080 --code PAIRING_CODE
```

Check that every real joint matches the URDF before the first powered move:

```bash
python scripts/joint_check.py --url http://JETSON_ADDRESS:8080 --code PAIRING_CODE
```

With the arm resting in its start pose and torque off, it compares the readings with the home angles in the code, then asks you to turn one joint at a time in a described direction (for example "so the gripper tip goes forward, toward the eyes"). It fails if a different joint reads the change (motor IDs or order wrong) or if the joint reads backwards (axis sign wrong, which would make IK and gravity compensation push the wrong way). Against a `--sim` server, `--simulate` moves the joints itself, and `--fault flip` or `--fault swap` plant a reversed J2 or swapped J4 and J5 to show the check catching them.

### Commands

The Commands box on the dashboard and `POST /api/command {"text": ...}` turn short phrases into joint moves: raise or lower hands (J1), raise elbows (J2), bend or straighten elbows (J3), raise wrists or turn wrists in or out (J4 or J5, whichever moves the tip that way from the current pose), twist wrists clockwise (J6), move hands forward 5 cm (IK, orientation held), open or close grippers, joint 3 by 10 or joint 3 to 90, wave, go home. Say left, right or both; no side means both. Join steps with then. Directions are worked out from the model at the current pose, so the mirrored arms both go the way the words say. Every step is planned and checked before anything moves, including keep out boxes around the post and head, and the arms move at 30 degrees per second (`REBOT_TELEOP_COMMAND_DEG_S`). The same keep out boxes now also stop Quest and phone teleop short of the post.

### Wrist cameras

Each arm card area gets a Wrist cameras panel. In `--sim` the picture is rendered from the camera on the UVC32 mount. On hardware pass the USB devices, for example `--camera left=/dev/video0 --camera right=/dev/video2`, and install OpenCV with `pip install .[camera]`. The stream is MJPEG at `/api/camera/left.mjpg`, with a single frame at `/api/camera/left.jpg`, and needs the same pairing as the dashboard. A camera is opened only while someone is watching.

### Watching the simulation from anywhere

The dashboard has a Simulation view card: a live picture of the whole simulated robot, rendered on the computer running `--sim`. Drag it to look around, or use Face, Left side, Right side, Back and Top. It works on a phone, so you do not need to be at the computer:

1. Start the simulator with `--lan`, for example `python -m rebot_teleop --sim --lan --data ~/.rebot-teleop-sim`.
2. **Tailscale** (phone on the same tailnet): open `http://<computer tailscale name or ip>:8080` on the phone.
   **Anywhere else**: add `--cloudflare` and open the `https://….trycloudflare.com` link it prints.
3. At the computer press Show pairing code and pair the phone. To send commands from the phone, also tick Allow phone and headset motion.

The picture is also at `/api/sim/view.jpg` and `/api/sim/view.mjpg` with `yaw`, `pitch`, `distance` and `height` query parameters; yaw 90 looks at the face.

### VR in the simulation

Start the simulator with `--lan` on Tailscale or with `--cloudflare`, open the Quest page from the pairing code on the headset, and the Quest controllers drive the simulated arms exactly as they would drive the real ones. Watch the 3D window or the Simulation view while you move. `scripts/validate_sim.py` runs the same checks without a headset.

### Agents and code as policy

Claude Code, Codex, Cursor, Antigravity, Gemini CLI and any other MCP client can drive the robot through `rebot_teleop.mcp_server`, in the style of Code as Policies: the agent reads the state, looks through the cameras, then writes a short Python policy against a `robot` object and runs it. Install the extra once with `pip install .[agent]`.

Tools: `get_state`, `move_arm` (joint angles, a tip position or a tip offset, with a dry run), `set_gripper`, `say` (the same phrases as the Commands box), `look` (wrist cameras or the overview picture, returned as an image), `run_policy` (Python, in its own process with a time limit; the arms stop if it fails), `home`, `stop` and `robot_api`. Every move goes through the joint limits, the floor check and the keep out boxes around the post and head before anything moves, and a refused move says why.

Agents only move the simulator. Against a server on real arms the tools refuse unless the person at the robot starts the MCP server with `REBOT_ALLOW_REAL=1`, and even then they cannot power the arms on; that stays a button on the dashboard.

Start the simulator first, then connect your agent with one of these. Use the full path to this repo's `.venv/bin/python`.

```bash
claude mcp add rebot -- /path/to/rebot-phone-teleop/.venv/bin/python -m rebot_teleop.mcp_server
```

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.rebot]
command = "/path/to/rebot-phone-teleop/.venv/bin/python"
args = ["-m", "rebot_teleop.mcp_server"]
tool_timeout_sec = 200
```

Cursor (`~/.cursor/mcp.json`), Antigravity (MCP servers, raw config) and Gemini CLI (`~/.gemini/settings.json`) take the same shape:

```json
{"mcpServers": {"rebot": {"command": "/path/to/rebot-phone-teleop/.venv/bin/python", "args": ["-m", "rebot_teleop.mcp_server"]}}}
```

Sandboxed installs, such as Codex or Antigravity from a snap, cannot start programs inside hidden folders like `.venv`. For those run the MCP server yourself over HTTP and give the agent its address instead (`url = "http://127.0.0.1:8765/mcp"` in Codex, `"url"` or `"serverUrl"` in the JSON clients, `claude mcp add --transport http rebot http://127.0.0.1:8765/mcp` in Claude Code):

```bash
python -m rebot_teleop.mcp_server --http 8765
```

To drive a simulator or robot on another computer, add `--url http://<that computer>:8080 --code <pairing code>`.

You can also write policies yourself. `python -m rebot_teleop.agent` prints the API, and `python -m rebot_teleop.agent policy.py` runs a file with `robot`, `np`, `math` and `time` ready:

```python
robot.home()
robot.move(left={"tip_xyz": [0.30, 0.06, 0.80], "gripper_m": 0.05},
           right={"tip_xyz": [0.30, -0.06, 0.80], "gripper_m": 0.05})
robot.move_tip_by("left", [0, 0, 0.05])
robot.say("wave left arm then go home")
```

World frame: x forward toward the eyes, y to the robot's left, z up, metres, origin at the foot of the post.

### Temperature

The dashboard shows every motor temperature. At 75 C it warns, at 100 C it stops motion and holds, and Reset works again once the motor is below 90 C.

Meshes come from Seeed's reBot description; set `REBOT_SIM_MESHES` to its `RS/meshes` folder if it is not found automatically. The wrist camera mount is Seeed's UVC32 mount.
