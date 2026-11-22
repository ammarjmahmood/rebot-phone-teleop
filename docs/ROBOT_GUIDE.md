# reBot robot guide for coding agents

Read this before moving anything. The `robot` API is printed by the `robot_api` MCP tool.

## The robot

A white post 1.30 m tall (6 cm deep, 4 cm wide) stands on the floor. On top sits a head with a phone showing two eyes. The eyes face forward. Since 2026-11-21 the two Seeed reBot B601 RS arms stand upright on a wooden shelf 20 cm in front of the post, 34 cm apart, with the shelf top about 0.82 m above the floor (set in `~/.rebot-teleop/layout.json`; the heights are design values until measured). Before that they were bolted to the sides of the post.

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
| home | upper arm straight up, forearm tucked down in front of the shelf, gripper pointing down | (0.42, 0.17, 0.78) | (0.42, -0.17, 0.78) |
| ready | hand a little further out and higher over the table, gripper pointing mostly down | (0.53, 0.17, 0.86) | (0.53, -0.17, 0.86) |

`robot.go("home")` and `robot.go("ready")` go there; poses saved on the dashboard work the same way. Moves are straight lines in joint space, so go up and over, then down. Home and ready both hold about 5 to 6 Nm on joints 2 and 3; there is no pose in front of the shelf that costs nothing.

Joint values (degrees): home left [-90, 170, 145, 30, 0, 0], right [90, 170, 145, 30, 0, 0]; ready left [-28.5, 107.4, 43.5, 74.3, 61.2, -9.1], right [28.5, 107.3, 43.3, 74.4, -61.1, 9.1]. The arms are mirror images: J1, J5 and J6 change sign between left and right, J2, J3 and J4 do not.

## What each joint does, starting from home

| Joint | Motion on the flat shelf | Limits |
|---|---|---|
| J1 | turns the whole arm about the upright axis, like a turntable | -158 to 158 |
| J2 | lifts the upper arm: 0 lies it flat pointing back at the post, 90 stands it straight up, 180 lies it flat pointing forward. Below about 0 the elbow meets the shelf | -60 to 240 |
| J3 | opens the elbow: 0 folds the forearm against the upper arm, 180 is straight | -3 to 178 |
| J4 | wrist pitch | -88 to 88 |
| J5 | wrist turn | -88 to 88 |
| J6 | spins the gripper about its own pointing axis | -179 to 179 |

You rarely need joints directly. Prefer tip positions (`robot.move_tip`) and let inverse kinematics pick the joints.

## The gripper

* The tip point (`tip_xyz`) is between the fingertips.
* The gripper points along its local x axis. The fingers close along its local y axis. Width runs from 0 (closed) to 0.05 m (open).
* `robot.top_down(arm, yaw_deg)` returns the hand orientation for pointing straight down. At yaw 0 the fingers close along world x; yaw turns that about world z. A block is easiest to grip when yaw matches the block's own yaw modulo 90 degrees, within -45 to 45.
* To grasp a block, put the tip at the block's centre height (table top + half the block size) and close.
* In the real robot the gripper reading stops short of fully closed when it holds something; in the simulator it does not, so check holding with a camera or by height.

## Where the arms can reach

* Each arm works on its own side: with the hand pointing down the left arm reaches y from about 0.10 to 0.40 m and the right arm y from about -0.10 to -0.40 m, at x from about 0.15 to 0.40 m at table height. The side mounts stop either arm from crossing the centre line; that is the joint limits, not software.
* Nothing is refused. A target out of reach moves as close as the arm can get and the result gives `reach_error_m` and a note. Use `robot.can_reach` to check first.
* There is no collision checking between the arms or with the post. The shelf the arms stand on is checked the same way as the table for planned moves, but not in teleop. The table is checked once its position is set (`POST /api/table` with `top`, its height in metres, and `front`, how far its near edge is from the post): a move whose path would take the gripper or wrist into it is refused with an error that says so. Moves are straight lines in joint space, so a move from home to a point over the table swings the gripper low through the table edge; go to ready first, then over, then down.

## Cameras

* `head`: Angstrong HP60C on the head with colour and aligned depth; `robot.locate("head", pixels)` uses the depth.
* `left` and `right`: wrist cameras; `locate` intersects the ray with the table plane (or `plane_z`).
* `robot.find_color(camera, color)` finds red, orange, yellow, green or blue objects and returns their world positions.

## Grip strength

The grippers have no force sensor. When the fingers stop on something the gripper motor eases off to a holding squeeze. `POST /api/limits` with `grip` between 0.1 and 1.0 (the Grip slider on the dashboard) sets how hard: lower closes slower, notices contact sooner and holds lighter. `gripping` in the arm state is true while the fingers are holding against something, and `grip_nm` is the motor's squeeze. Opening is always full strength.

## Motor budget

On the flat shelf joint 2 carries the whole arm whenever it reaches forward (on the old side mount it was joint 1). In the model a hand within 50 cm of the post holds forever, 60 cm out costs about 11 Nm (2 to 3 minutes) and 70 cm out about 14 Nm (under a minute). A RobStride RS06 holds about 8 Nm for as long as you like while it is still, 11 Nm for about 200 s and 17 Nm for about 15 s (RobStride locked rotor table). Joints 4 to 6 (RS00) hold 5 Nm. The server tracks how much of that each joint has used (`heat` in the arm state, `robot.budget(arm)` in a policy).

* The ready pose costs joint 1 about 7.5 Nm, nearly all of its budget. Reaching straight out costs about 16.6 Nm, which lasts seconds.
* At 80% (95% with heavy poses on) the arm pulls the hand straight back toward the robot at the same height or higher, never down onto the table, until joint 1 is at about 60% of what it holds, then hands control back. The budget then recovers on its own: about 1.5 to 2.5 minutes from 80% to 50% if the hand stays light, much longer if it stays near the limit. Moves are refused only while it is pulling in; `robot.wait_ready()` waits for it. Sequence rests wait there for the budget, then go back to the step.
* Work quickly, keep the hand close in or low, and park at home, which costs almost nothing.
* Every move returns `load`, the torque each joint will hold at its goal, and `load_note` when a joint would hold more than it can for long and how many seconds that leaves. Holding a 0.8 kg jar reached out puts joint 1 at 15 to 19 Nm, which lasts 13 to 30 s: pick, move and set objects down rather than holding them out.
* In teleop, once a joint has used half its budget the arm stops following the hand further out than that joint can hold; the headset and dashboard say "reach limited". Bring the hand in and it follows again.
* `robot.motor_report()` (MCP `motor_report`) shows which poses used the budget most and least over the last day.
* Heavy poses switch (dashboard, `robot.heavy(True)`, MCP `set_heavy_poses`): no reach limit in teleop, no rests or refusals in sequences, and the arm relaxes only at 95% of the budget instead of 80%. Moves are never refused for load in either mode; they only report it.
* The Sequencer checks every sequence against the budget (`plan_sequence`, or `GET /api/seq/<name>/plan`) and rests the arms before any step that would run out.

## Habits that work

* Start with `look_all`, write one policy for a whole step (approach, grasp, lift), run it, then `look_all` again.
* Grasp at the object centre height with the gripper pointing down (`rotation="down"` or a yaw in degrees). The gripper opens to 5 cm.
* If a policy fails the arms hold where they are; read the error, adjust, run again.
