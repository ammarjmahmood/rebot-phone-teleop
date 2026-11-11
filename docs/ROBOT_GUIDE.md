# reBot robot guide for coding agents

Read this before moving anything. The `robot` API is printed by the `robot_api` MCP tool.

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

`robot.go("home")` and `robot.go("ready")` go there; poses saved on the dashboard work the same way. Moves are straight lines in joint space, so from home go to ready first before reaching over the table.

Joint values (degrees): home left [-90, 170, 145, 30, 0, 0], right [90, 170, 145, 30, 0, 0]; ready left [-28.5, 107.4, 43.5, 74.3, 61.2, -9.1], right [28.5, 107.3, 43.3, 74.4, -61.1, 9.1]. The arms are mirror images: J1, J5 and J6 change sign between left and right, J2, J3 and J4 do not.

## What each joint does, starting from home

| Joint | Motion from the hanging pose | Limits |
|---|---|---|
| J1 | swings the whole arm forward and up, like raising a hand in front of you (left arm: more negative is forward; right arm: more positive) | -158 to 158 |
| J2 | swings the elbow out sideways, away from the post | -3 to 178 |
| J3 | bends the elbow; from home it folds the forearm inward toward the post, so bend it only once the arm is out from the post | -3 to 178 |
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
* There is no collision checking between the arms, the post or the table. Plan moves that stay clear, go up before going across, and move in a few larger steps.

## Cameras

* `head`: Angstrong HP60C on the head with colour and aligned depth; `robot.locate("head", pixels)` uses the depth.
* `left` and `right`: wrist cameras; `locate` intersects the ray with the table plane (or `plane_z`).
* `robot.find_color(camera, color)` finds red, orange, yellow, green or blue objects and returns their world positions.

## Motor budget

The arms hang off the side of the post, so joint 1 carries the whole arm whenever it reaches forward. A RobStride RS06 holds about 8 Nm for as long as you like while it is still, 11 Nm for about 200 s and 17 Nm for about 15 s (RobStride locked rotor table). Joints 4 to 6 (RS00) hold 5 Nm. The server tracks how much of that each joint has used (`heat` in the arm state, `robot.budget(arm)` in a policy).

* The ready pose costs joint 1 about 7.5 Nm, nearly all of its budget. Reaching straight out costs about 16.6 Nm, which lasts seconds.
* At 80% the arm relaxes at home by itself, waits until the motors recover (about 2 to 3 minutes) and then goes back to where it was. Moves are refused while it relaxes; `robot.wait_ready()` waits for it.
* Work quickly, keep the hand close in or low, and park at home, which costs almost nothing.
* The Sequencer checks every sequence against the budget (`plan_sequence`, or `GET /api/seq/<name>/plan`) and rests the arms before any step that would run out.

## Habits that work

* Start with `look_all`, write one policy for a whole step (approach, grasp, lift), run it, then `look_all` again.
* Grasp at the object centre height with the gripper pointing down (`rotation="down"` or a yaw in degrees). The gripper opens to 5 cm.
* If a policy fails the arms hold where they are; read the error, adjust, run again.
