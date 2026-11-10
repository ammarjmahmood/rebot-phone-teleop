# reBot robot guide for coding agents

Read this before moving anything. It describes the robot, its frames, what each joint does, where the arms can reach, how to see, and how to work safely. Everything here is true for the simulator and for the real robot unless a section says otherwise.

## The robot

A white post 1.30 m tall (6 cm deep, 4 cm wide) stands on the floor. On top sits a head with a phone showing two eyes. The eyes face forward. Two Seeed reBot B601 RS arms are bolted to the left and right sides of the post, 15.5 cm below its top, so each shoulder is at about 1.145 m.

Each arm has six joints (J1 to J6) and a parallel gripper. A USB camera on each wrist looks down past the gripper.

## World frame

All positions are metres in one world frame:

* origin on the floor at the foot of the post
* **x** forward, the way the eyes look
* **y** to the robot's own left (the left arm is at y > 0, the right arm at y < 0)
* **z** up

"Left" and "right" always mean the robot's left and right, not the viewer's. Seen from the front, the robot's left arm is on your right.

## Poses

| Pose | What it looks like | Left tip | Right tip |
|---|---|---|---|
| home | arms hanging straight down beside the post | (0.00, 0.20, 0.37) | (0.00, -0.20, 0.37) |
| ready | forearms reaching forward over the table, grippers pointing down and open | (0.30, 0.25, 0.78) | (0.30, -0.25, 0.78) |

`robot.home()` and `robot.ready()` go there. Home is where the real robot rests when powered off. The straight path from home to anything over the table hits the table, so the planner finds a detour automatically; you never need to plan that yourself.

Joint values (degrees): home left [-90, 170, 145, 30, 0, 0], right [90, 170, 145, 30, 0, 0]; ready left [-28.5, 107.4, 43.5, 74.3, 61.2, -9.1], right [28.5, 107.3, 43.3, 74.4, -61.1, 9.1]. The arms are mirror images: J1, J5 and J6 change sign between left and right, J2, J3 and J4 do not.

## What each joint does, starting from home

| Joint | Motion from the hanging pose | Limits |
|---|---|---|
| J1 | swings the whole arm forward and up, like raising a hand in front of you (left arm: more negative is forward; right arm: more positive) | -155 to 155 |
| J2 | swings the elbow out sideways, away from the post | -3 to 175 |
| J3 | bends the elbow; from home it folds the forearm inward toward the post, which is why bending at home is refused | -3 to 175 |
| J4 | wrist pitch | -85 to 85 |
| J5 | wrist turn | -85 to 85 |
| J6 | spins the gripper about its own pointing axis | -179 to 179 |

You rarely need joints directly. Prefer tip positions (`robot.move_tip`) and let inverse kinematics pick the joints.

## The gripper

* The tip point (`tip_xyz`) is between the fingertips.
* The gripper points along its local x axis. The fingers close along its local y axis. Width runs from 0 (closed) to 0.05 m (open).
* `robot.top_down(arm, yaw_deg)` returns the hand orientation for pointing straight down. At yaw 0 the fingers close along world x; yaw turns that about world z. A block is easiest to grip when yaw matches the block's own yaw modulo 90 degrees, within -45 to 45.
* To grasp a block, put the tip at the block's centre height (table top + half the block size) and close.
* In the real robot the gripper reading stops short of fully closed when it holds something; in the simulator it does not, so check holding with a camera or by height.

## Where the arms can reach

This matters more than anything else when planning.

* Each arm only works on its own side. With the hand pointing down, the left arm reaches y from about 0.10 to 0.40 m and the right arm y from about -0.10 to -0.40 m, at x from about 0.15 to 0.40 m, at table height.
* **There is no shared workspace.** Nothing between y = -0.10 and 0.10 can be reached from above, and the two grippers cannot meet in the air in front of the robot. Do not plan handovers. Work with the arm on the object's side.
* Higher points need the hand closer to the post: at 0.85 m height only y beyond about 0.30 m is reachable top down.
* `robot.can_reach(arm, xyz, rotation)` checks a point without moving.
* Joint J4 often sits near its 85 degree limit over the table. If a top down pose is out of reach by a little, the planner tilts the hand slightly and says so in the move result.

## The table (simulator tasks)

Top at z = 0.68 m, x from 0.12 to 0.55 m, y from -0.40 to 0.40 m. Objects in tasks spawn at x 0.24 to 0.36 and |y| 0.16 to 0.32. Blocks are 3 cm cubes. Bins are 11 cm square inside, 4.5 cm tall, open at the top.

On the real robot, describe the actual table to the server with `REBOT_TELEOP_TABLE='{"top": 0.68, "x": [0.12, 0.55], "y": [-0.4, 0.4]}'` so the planner avoids it.

## Safety the server enforces

Every move is checked before anything moves: joint limits, the floor, keep out boxes around the post, the head and the table (the arm links keep 3.5 cm away; only the fingertips may come within a few millimetres of the table), and a smooth path at a capped speed. A refused move raises `RobotError` and explains why; nothing moves in that case. The planner does not check one arm against the other, so keep each arm on its own side.

## Seeing

* `robot.capture("left" | "right" | "overview")` returns an RGB image and its camera info: `fx, fy, cx, cy, width, height` and `world_from_camera`, a 4x4 pose with OpenCV axes (x right, y down, z forward).
* `robot.locate(camera, [(u, v)])` turns pixels of the last capture into world points. In the simulator it uses the depth image. On the real robot the cameras have no depth, so it intersects the pixel ray with a horizontal plane, by default the table top; pass `plane_z` for other surfaces.
* `robot.find_objects(camera, "red", z_range=(0.67, 0.80))` does capture, colour blobs and 3D location in one call. Parts of the robot itself (the collar under the head, the head, the arms) can match a colour, so always filter by height or position.
* The overview camera exists only in the simulator. It is the easiest way to find objects there. On the real robot use the wrist cameras: move to the ready pose, capture, locate, then refine by capturing again from above the object.
* Each wrist camera sits about 12 cm above the tip and looks down and inward toward the middle. Hold still while capturing.
* The real robot can also have a fixed `torso` camera low on the post that sees both arms and the table at once. Use it to find objects and to check where each gripper is relative to them, then use the wrist cameras for the close approach. `locate` works on it only when its position is set with `--camera-pose torso=X,Y,Z,LOOK_X,LOOK_Y,LOOK_Z`; otherwise locate through a wrist camera.
* The real wrist cameras are Innomaker USB (UVC) cameras. Their field of view is set with `REBOT_TELEOP_CAMERA_FOV` (horizontal degrees, default 80). Measure it once for accurate locations.

## Working on a task (develop, freeze, evaluate)

This follows the RoboDojo code as policies protocol.

1. **Develop.** `robot.task(name, seed)` resets a task layout. Use development seeds 0 to 99. Look, write a policy in `policies/<task>.py`, run it with `python -m rebot_teleop.agent policies/<task>.py` or the `run_policy` tool, check `robot.task_status()`, improve. About ten attempts per task is the norm.
2. **Freeze.** Stop editing the policy file.
3. **Evaluate.** `python -m rebot_teleop.dojo eval <task>` runs the frozen file on evaluation seeds 100 to 119 with no agent involved and ground truth hidden. Results go to `results/`. `python -m rebot_teleop.dojo summary` prints the table.

Policies must find objects with the cameras. `robot.objects()` gives ground truth positions and is fine while developing, but it raises an error during evaluation.

Tasks: `lift_block`, `block_in_bin`, `stack`, `sort_colors` (both arms), `lift_both` (both arms at once). `python -m rebot_teleop.dojo list` prints their instructions.

## Rules for agents

1. Work in the simulator first. Check `robot.state()["sim"]`.
2. The real arms move only when the person at the robot has started the MCP server with `REBOT_ALLOW_REAL=1`, the arms are powered from the dashboard, and the person has agreed to that specific motion. Describe what will move before running anything on hardware. Agents can never power arms on.
3. Keep moves slow near objects (`speed_deg_s=15`) and use `robot.stop()` if anything looks wrong.
4. After each motion, look before deciding the next one. Do not assume a grasp worked.
5. Policies are plain Python with `robot`, `np`, `math` and `time` available. Print what you observe; the output is how you learn what happened.

## Common refusals

| Message | Meaning |
|---|---|
| The arm would hit the post / head / table | the goal or the path passes too close; choose another point or approach from above |
| hand cannot reach [x, y, z] | outside the workspace for that orientation; check the reach section, try the other arm or tilt the hand |
| Joint N limit reached | the pose needs a joint past its limit |
| No collision free path found | no detour found in 1.5 s; go to `robot.ready()` first, then try again |
| Power the arm on first | in the simulator the client powers on by itself; on hardware a person must press Power on |
