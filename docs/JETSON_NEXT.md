# Continuing on the Jetson

This is the handoff from the PC session of 2026-11-09. A new Claude Code session on the Jetson (or the PC) should read this file, check the state below against the code, and carry on from "Steps left". Ask the person before every step that moves or powers a real arm.

## State

Branch `sim/bimanual-mount` on `github.com/ammarjmahmood/rebot-phone-teleop`. Everything below is pushed.

Built and checked in the PyBullet simulator on the PC (RTX 5080):

* Bimanual simulator of the post, head and two B601 RS arms, gravity compensation aware of the side mounts, temperature warnings and stops.
* Quest, phone and HEBI teleop; `scripts/validate_sim.py` passes 24 of 25 (the known failure is right arm roll).
* Hand guiding with torque off and `scripts/joint_check.py`, which checks every real joint's order and direction against the URDF (28 of 28 in the simulator, and it catches a reversed or swapped joint).
* Typed commands ("raise left hand", "bend elbows", "get ready", "wave").
* Agent interface: `rebot_teleop/agent.py` (the `robot` object), `rebot_teleop/mcp_server.py` (MCP tools), `.mcp.json` so Claude Code in this folder gets the tools, `docs/ROBOT_GUIDE.md` which the agent reads.
* Motion planning around the post, head and table (`Kinematics.route`), with the table as a keep out zone.
* Cameras for policies: `robot.capture`, `robot.locate` (pixel to world; depth in the simulator, table plane on hardware), `robot.find_objects`.
* RoboDojo style tasks in the simulator (`rebot_teleop/tasks.py`, `python -m rebot_teleop.dojo`): lift_block, block_in_bin, stack, sort_colors, lift_both. Quick reference policies using ground truth positions scored 3 of 3 on block_in_bin, stack and lift_both and 2 of 3 on sort_colors (one drop missed the bin), so every task is solvable. `policies/baseline/lift_block.py` passes 10 of 10 evaluation seeds using only the overview camera.
* Dashboard Simulation view, viewable from a phone over Tailscale (`http://amarsbar.tail6b807a.ts.net:8080` when the PC runs `--sim ... --lan`) or a Cloudflare link.

Not done yet:

* Fable itself has not driven the robot. Claude Code on the PC had an expired login.
* Nothing has run on the real arms. The joint check, home angles and IK branch are unconfirmed on hardware.
* The real wrist cameras (Innomaker USB) have not been opened; their `/dev/video` numbers and field of view are unknown.
* The real robot has no overview camera, so camera only policies on hardware must use the wrist cameras.

## Jetson state as of 2026-11-09 22:45

* The repo is on `sim/bimanual-mount` (commit `335047b` or later). Uncommitted work found on `main` (mount specs such as `--mount left=side_left@1.15`, `--gravity-scale`, pairing and motion windows, a guard in `app.js`, a Clear fault button) was saved to the local branch `jetson/mount-wip` (`241e93b`) and to `~/jetson-mount-wip-20261109.patch`. The pairing and motion window options were brought into this branch; the rest is waiting for a decision.
* `rebot-teleop.service` (systemd, enabled at boot, `Restart=always`) runs `python -m rebot_teleop --lan --port 8081 --https-port 8443 --pairing-ttl 86400 --motion-window 2700 --arm left=can1 --arm right=can2` from this folder. It was still running the old code when this was written; `sudo systemctl restart rebot-teleop` loads the new code. Both arms were not answering on can1 and can2 at the time (no power or no CAN link).
* The service does not pass `--mount`, so this branch's default side mounts apply, which is what the post assembly needs.
* Agent packages (`.[agent,camera]`) are installed and import cleanly; 29 tests that do not need PyBullet pass on the Jetson.
* Seeed's arm meshes were copied to `~/rebot-phone-teleop/meshes` (ignored by git).
* PyBullet 3.2.7 was compiling from source; check with `.venv/bin/python -c "import pybullet"` and rerun `~/.local/bin/uv pip install --python .venv/bin/python pybullet==3.2.7` if it is missing.
* Claude Code is not installed yet: `curl -fsSL https://claude.ai/install.sh | bash`, then `claude login`.
* No `/dev/video` devices were present, so the wrist cameras were not plugged in.

## Where to run what

* **Simulator: on the PC.** PyBullet renders camera pictures on the PC's GPU (EGL) in about 5 ms. On the Jetson PyBullet has no prebuilt wheel and would render on the CPU at about 250 ms a frame, which stalls the physics.
* **Real robot server: on the Jetson** (CAN to the motors, cameras on USB).
* **Claude Code: either.** The model runs in the cloud. Point the tools at a server with `REBOT_URL` (and `REBOT_CODE`, a pairing code, when the server is on another computer).

## Steps left

1. **Pull and set up** (no motion):
   ```bash
   cd ~/rebot-phone-teleop && git fetch && git checkout sim/bimanual-mount && git pull
   scripts/setup_agent.sh
   curl -fsSL https://claude.ai/install.sh | bash   # only if claude is missing
   claude login
   ```
2. **Try Fable on the simulator first.** Start the simulator on the PC (`.venv/bin/python -m rebot_teleop --sim headless --lan`), show a pairing code on its dashboard, then on the Jetson:
   ```bash
   REBOT_URL=http://amarsbar.tail6b807a.ts.net:8080 REBOT_CODE=<code> claude --model claude-fable-5-2
   ```
   Ask it to read `docs/ROBOT_GUIDE.md`, then develop `policies/stack.py` on seeds 0 to 9 and evaluate it with `python -m rebot_teleop.dojo eval stack`. Record the scores in this file.
3. **Find the cameras** (no motion): `ls /dev/video*`, `v4l2-ctl --list-devices`. Start the real server with `--camera left=/dev/videoN --camera right=/dev/videoM` and check both pictures on the dashboard.
4. **Joint check on the real arms** (motors off, arm supported, ask the person first): `python scripts/joint_check.py`. Fix any reversed or swapped joint before anything else. If the rest pose differs from the code, save the readings as the home angles.
5. **Camera field of view** (no motion): photograph a ruler at a known distance with each wrist camera, compute the horizontal field of view and set `REBOT_TELEOP_CAMERA_FOV`.
6. **Table** (no motion): measure the real table (top height, x and y extent in the world frame of `docs/ROBOT_GUIDE.md`) and set `REBOT_TELEOP_TABLE`.
7. **First powered agent run** (only with the person watching, hand on STOP, and their explicit OK for each step): start the MCP server with `REBOT_ALLOW_REAL=1`, the person presses Power on, then try `robot.ready()` and a single slow `robot.pick` of a block whose position the person confirms.

## Facts worth knowing

* The two arms share no workspace (left reaches y 0.10 to 0.40 m, right y -0.10 to -0.40 m from above). Handovers are not possible with this mount.
* Parts of the robot can match an object colour (the collar under the head showed up as red); filter detections by height or position.
* PyBullet's own GUI window does not draw bodies added after it opens, so the simulator shows its view on the dashboard instead.
* The simulator's gripper reading does not stop on contact, so holding is checked by object height or cameras there.
