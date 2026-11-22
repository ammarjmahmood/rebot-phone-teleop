# reBot log

What happened on the real robot, what we measured and what we learned. Newest first.

Raw data for each day sits in `~/.rebot-teleop/incidents/<date>/` on the Jetson (the motor log in `~/.rebot-teleop/motorlog/` is deleted after 14 days, so copy anything worth keeping).

## 2026-11-11 evening: connection trouble, sequencer fixes, step 6 too heavy

Raw data: `~/.rebot-teleop/incidents/2026-11-11/`

| File | What it is |
|---|---|
| `motorlog.jsonl` | both arms at 5 Hz: joint angles, torque, velocity, heat budget, events |
| `arm_events.log` | every arm message from the server log, 19:00 to 23:00, in time order |
| `usb_events.log` | kernel USB and CAN adapter plug and unplug messages |
| `runs.csv` | the three playback attempts below, row by row |
| `Sequence 1.json` | the sequence as it stood after the runs |
| `sequence1_motor_check.json` | the motor check (plan) for that sequence |

### Connection

| Time | What happened | Cause | Fix |
|---|---|---|---|
| 19:54 | Both arms vanished, then power on failed with "No such device" | The external USB hub (port 1-2.1, Genesys 05e3:0610) dropped out with USB error -71, taking both PCAN adapters and both wrist cameras with it | Operator plugged the hub back in at 20:49. It had come unplugged |
| 20:53 | Adapters back but "No motors answer" | Arm power supply off | Operator switched it on |
| 20:53 | Left arm: joints 1 to 6 answer, gripper motor (id 7) silent | Gripper cable. A direct ID ping (`cansend can2 0000FD07#`) got no reply after rewiring | Still open |
| 20:56 | Both PCAN USB cables dropped out of the hub within 3 s | Cables nudged while working at the robot; they come loose easily | Replugged. Secure them |
| 21:01 | Right arm: all 7 motors answering | Cable at the right shoulder reseated | |
| 21:33 to 21:46 | Arms connected and disconnected dozens of times | Loose power or CAN leads while the operator worked on them | |
| 21:52 | Left arm lost all motors | Power to the left arm | Still offline at the end of the night |

CAN names swap when adapters replug (the right arm was can2, then can1). The server maps arms by USB port in `~/.rebot-teleop/can_ports.json`, so this is harmless.

### Sequencer problems the operator reported

1. Arms moved "on their own to random places" during playback.
2. Upward motions recorded by hand did not play back.
3. "Limited to three recordings".

Findings and fixes (commit 4498522 on branch `fix/sequencer-playback`):

* **Whole turn jumps (cause of 1).** Joint 2 on the right arm read about 180 degrees, then about -151 after a reconnect: the same place, one turn apart. The arm took the jump as real and drove the joint hard to "correct" it. Reproduced in the simulator: the old code shoved the joint 171 degrees. Fix: a reading that jumps about 360 degrees between samples is treated as the same position, and the jump is logged ("reading jumped ... treating it as the same position").
* **Recordings past the joint limits (cause of 2).** Joint 3 (elbow) reached 227 to 243 degrees while recording by hand; playback clips it to 178, so those moves came out flattened. Fix: the Sequencer tab warns live while recording, and the motor check tags saved steps "cut short".
* **Safety rests looked random (also 1).** When a joint uses up its heat budget the arm pulls in to rest by itself, which looks like unrecorded motion. Fix: the Sequencer tab now says when an arm is moving on its own and why.
* **Three recording limit.** Not found in the server or page code. Most likely recordings refused while an arm was offline during the connection churn above.

### Playback attempts

All on the right arm. `runs.csv` has the row by row data.

| Time | What was played | Result | Peak shoulder (J1) | Peak J2 |
|---|---|---|---|---|
| 22:28 | Sequence 1 from step 1 at half speed | Every motor cut out 3.6 s in (undervoltage on J1 to J3, overcurrent on J4 to J6) on the way to the saved start pose, which has J2 at 205 degrees. J2 was at 196 to 198 when it hit full force | 36.0 Nm | 36.0 Nm |
| 22:46 | Step 6 only, full speed, Heavy poses on | J1 used up its budget at about 5.5 s, could not pull in, then cut out (overcurrent, stall overload, fault 0x4020) at about 6.5 s. Arm stuck about 50 cm out at table height; operator lowered it | 35.2 Nm | 6.4 Nm |
| 22:51 | Step 6 again, J1 still at 115% budget and 41 C | J1 used up its budget at 4 s and held part way out; switched off from the dashboard at 22:51:54. No motor cut out | 12.7 Nm | 6.4 Nm |

Earlier the same pattern as 22:28 hit at 21:49 and 21:52: J2 near or past 180 degrees, J1 and J2 at their 36 Nm peak, then undervoltage on every motor.

### Why step 6 cannot play

Step 6 (recorded 22:36:59, 26.7 s) holds the hand about 72 cm in front of the post at about 85 cm high for roughly 14 s. The gravity model puts joint 1 at about 17 to 18 Nm for that whole hold. Joint 1 (RS06) can hold about 8 Nm for long and peaks at 36. It lasts 4 to 6 s, then gives out. Switching the safety check off (Heavy poses) does not change this; it only removes the refusal.

| Step | Recorded | Shoulder load | Verdict |
|---|---|---|---|
| 1 | 21:43 point | light | J2 at 205; this is what cut the power at 22:28 |
| 2 | 21:49 motion | light | elbow cut short (243) |
| 3 | 21:56 motion | light | elbow cut short (227) |
| 4 | 22:31 motion | at most 4 Nm | fine (small elbow and wrist cut) |
| 5 | 22:34 motion | over 8 Nm for about 7 s, peak 18 | passes the check, only just |
| 6 | 22:36 motion | over 8 Nm for about 14 s, peak 18 | too heavy |

### Lessons

1. **Keep the hand within about 40 cm of the post and low.** Reaching far forward or up is what overloads joint 1. A far reach is fine for a second or two, not for a hold.
2. **Keep joint 2 at or under 180 degrees and joint 3 under 178.** The maker rates joint 2 for 0 to 180; this software allows 240. Every power cut tonight happened with joint 2 near or past 180.
3. **A tired shoulder fails sooner.** The second try at step 6 gave out at 4 s instead of 6. Give it about 7 minutes (the budget recovers 100% per 300 s) between heavy tries.
4. **The power supply sags under heavy load.** When J1 or J2 hits its 36 Nm peak, every motor on that arm can drop out with undervoltage at once. A stiffer supply would stop the cut outs, but not the overload itself.
5. **Look before playing.** The depth camera put the honey jar right in step 6's path (76 cm out, top at 84 cm). It was not hit only because the arm gave out first.
6. **Secure the USB hub and PCAN cables.** Most of the evening's lost time was cables coming loose.

### Open items

* Left gripper motor (id 7) does not answer.
* Left arm offline since 21:52 (no power reaching it).
* Consider capping joint 2 at 180 degrees in `config.py` (now -60 to 240).
* Stiffer arm power supply.
* Merge `fix/sequencer-playback` into `main`.


## 2026-11-21 Flat shelf mount goes into the software

The operator built the flat shelf from `hardware/mount_options/BUILD_FLAT.md` and asked for the model to match.

* Both PCAN adapters were seen (can1 left, can2 right). No motors answered because the arm power was off. The head camera and the lift controller were not plugged in.
* `~/.rebot-teleop/layout.json` written: mounts level at (0.20, plus or minus 0.17, 0.818), home [0, 90, 40, -40, 0, 0], ready [0, 80, 45, -30, 0, 0], joint 2 limits -3 to 183, relief pull in to x 0.42, shelf obstacle, boards for the twin. `table.json`: top 0.68, front 0.31. Server restarted with the arms off.
* Code: `robot.py` reads `joint_limits_deg` and `shelf` from the layout; planned moves into the shelf are refused. 42 tests pass.
* Simulator (port 18083): 24 hand positions over the table all reached within 2 mm, teleop driven in every direction, no faults. Loads match the mount study: joint 2 holds 5 to 9 Nm within 50 cm of the post, 11 to 14 Nm at 60 to 70 cm.
* Later the same day the operator asked for the joint 2 limit to be removed; `joint_limits_deg` was taken out of the layout, so joint 2 is back to the software default of -60 to 240.
* Not checked on the robot: nothing has moved. Shelf height, base positions and table edge are design values, not measured. Heavy poses is still on.
* The old sequence was moved to `~/.rebot-teleop/sequences-old-side-mount/`.

* Grip strength added the same day at the operator's request, so the grippers stop crushing soft things. There is no force sensor; the gripper motor's position lag is used as the squeeze. `arm.py` scales the closing speed, the contact threshold and the holding squeeze by a strength from 10% to 100% (`/api/limits` with `grip`, saved in `limits.json`, slider on the dashboard). The Quest controller buzzes when the fingers meet something. Simulator, fingers blocked: holding squeeze 1.05 Nm at 100%, 0.31 Nm at 30%, 0.10 Nm at 10%; first touch peak 3.6, 1.6 and 1.0 Nm; full close takes 0.7, 1.5 and 2.3 s. The real robot is set to 30%. Not tried on the real grippers: the true force at the fingers and whether low settings stall in free air from friction are unknown. 43 tests pass.

* Enable buttons on the VR page added the same day at the operator's request. `/quest` now has Enable both arms, Enable left and Enable right; inside VR the A button on the right controller does the same and the panel under the camera view shows "press A" next to an arm that is off. They send `enable` on `/ws/teleop`, which powers on only arms whose torque is off (it tries to connect an arm that was offline) and holds them where they are. The page also shows live arm state before Enter VR. Tried in a headless browser on the port 18083 simulator; the A button itself was not pressed in a headset. 43 tests pass.

* Reset to home on the VR page added the same day at the operator's request. `/quest` has a Reset to home button; inside VR, holding A for two seconds with both grips released does the same (a short press of A still enables). It sends `home` on `/ws/teleop`: refused while a grip is squeezed or when no arm is enabled, otherwise every enabled arm goes to the home pose at 45 deg/s (`HOME_BUTTON_DEG_S`) with the usual table and shelf path check. Squeezing a grip takes over again. Tried on the port 18083 simulator in a headless browser with a pretend controller; not tried in a headset or on the arms. 43 tests pass.

* Teleop made smoother the same day, after the operator asked several times whether motion is smooth. Measured in the simulator at every 200 Hz tick with a hand sweep: the arm speed wobbled between VR frames (typical 7 deg/s at steady 72 Hz frames, 11 deg/s with uneven frames, acceleration at its limit about 30% of the time). `arm.servo` now takes a glide time and `teleop.py` glides each new hand frame in over 1.5 frame gaps (`TELEOP_GLIDE_FRAMES`, capped by `TELEOP_GLIDE_MAX_S`); STOP or any other command ends a glide at once. After: wobble 0.7 and 1.5 deg/s, acceleration never at the limit, lag behind the hand 30 to 35 ms instead of 20 ms. Not tried on the arms.
* Gripper in teleop checked in the simulator at 30% grip: VR trigger and phone slider both close to 0 mm and open to 50 mm on both arms. Teleop now forgets its last gripper width when a session starts or an arm comes back on, so a released trigger opens a gripper that something else closed. The known hardware cause of a dead gripper is still open: the left gripper motor (id 7) did not answer on 2026-11-11.
* The Sequencer tab is now called Record and was rebuilt at the operator's request. Save point, Record motion, and Open and Close gripper buttons that move the gripper at once and add the step (arm can be left, right or both). The page no longer sends or fetches the recorded motion data on every edit (`/api/seq/<name>?light=1`, `/edit`, `/rename`; a 56 kB recording is 880 bytes to the page), edits show at once, and typing is not interrupted by redraws. The layout wraps on a phone. Playback: recorded motion is resampled to 100 Hz, smoothed with a 60 ms Gaussian and eased in and out over 0.2 s (`condition` in `sequencer.py`); points and recordings store the commanded gripper width, not the measured one, so a replay squeezes instead of stopping at the object's width; gripper steps wait until the gripper has arrived or is holding something (up to 4 s) instead of a fixed 0.6 s. Tried in a headless browser on the port 18083 simulator. 46 tests pass. Not tried on the arms.

* First readings from the real arms on the flat shelf, the same evening (torque off, nothing moved). Right arm (can2): joints 1 to 6 answer, gripper motor 7 does not ("no gripper"). Left arm (can1): no motor answers; the adapter is up but only 2 frames in 2 s on the bus against 266 on can2. Cameras: both wrist cameras idle with no picture, head camera not plugged in, lift controller not plugged in.
* The operator said the pose the arms were resting in is the home position. Right arm read [-1.46, -0.21, -0.24, 0.52, -1.58, 181.47]: joints 2 and 3 sit at Seeed's lower limit of 0, so the arm is folded on its stops. `~/.rebot-teleop/layout.json` home is now right [-1.46, -0.21, -0.24, 0.52, -1.58, null] and left all null (old file kept as `layout.before_folded_home.json`). A null in a pose means that joint stays where it is (`Robot.pose_joints`, `C.pose_numbers`). The left arm's home is not known until it answers. The wrist roll is left out on purpose: it reads 181.47, past the 179 limit and at the wrap point, so after a motor power cycle it may read about -178.5 and a home move could turn the wrist almost a full turn. Open question for the operator: turn the wrist away from that end, or re-centre the wrist range. The model says joint 3 would hold 7 Nm in this pose because it does not know about the stops; to be compared with the real motor load at first power on. 47 tests pass.

* 20:42 the operator pressed Power on for the right arm twice and got "enable_all failed: control ack timeout: comm_type=3". Cause: the right gripper motor (7) does not answer but stays registered on the controller, and the library's `enable_all` waits for every registered motor. The simulator never failed there, so the no gripper test passed wrongly. Fix in `arm.py`: an arm without its gripper switches its six motors on and off one by one (`_motors_on`, `_motors_off`), and a power on that fails part way switches off whatever it had switched on. `simbus.py` now fails like the real library. The operator heard an arm energise during the failed tries, so some of motors 1 to 6 may have been left on while the dashboard showed torque off; a power on followed by power off, or cutting the arm supply, clears that. Not yet confirmed on the arm. 47 tests pass.

* About 20:55 the operator's second Power on of the arm on can2 worked with the one by one fix (torque on, six joints, no gripper). The operator then said that arm is physically the left one. `can_ports.json` left and right were swapped (old file `can_ports.before_swap.json`) and the saved home moved to the left arm; the right arm's home is now all null. Takes effect at the next server restart, which waits for the operator to switch torque off. Wrist camera names were not checked.

* First real teleop on the flat shelf, 20:50 to 21:00 (Quest). Both arms powered and followed with no lasting fault. Measured motor loads while holding matched the model (left folded: joint 3 at 7.0 Nm real, 7.0 Nm model), so the flat mount model is right and the folded home is not resting on a stop. Events: 20:52:55 the right arm stretched out (joint 2 at 175 deg holding 16.1 Nm), used up joint 2's budget and pulled itself in, which the operator saw as the arms going home by themselves. 20:53:30 and 20:53:41 the wrist motors 4, 5, 6 (and 7 on the right) of both arms reported overcurrent within 11 s of each other at almost no load (0 to 2 Nm), which looks like a supply dip rather than real overload; they were reset at 20:54:44. Around 20:59 the right arm logged 644 "joint 2, 3 is blocked" lines in a minute while folded: teleop was pressing joints 2 and 3 into their ends near 0 deg. The simulator on port 18083 had been left running and pulled the arm loops down to about 70 Hz; stopped, they ran 150 to 170 Hz (target 200), cause of the remaining gap not yet found.
* Operator requests the same evening: the wrist camera names were crossed, so `cameras.json` now sets the devices the other way round (old file `cameras.before_swap.json`); the right arm's resting pose [1.38, 0.02, -0.09, -0.62, -7.08, -6.2] is saved as its home (`poses.json` now, `layout.json` for the next start).
* Code as policy from the folded home: the planner got stuck (10 of 27 test points with the hand angle kept, joint 2 driven to -60 deg). `Unit.solve` now retries from upright arm shapes and, if the hand angle cannot be kept, puts position first; `layout.json` limits joint 2 to -1..180 and joint 3 to -1..178 (Seeed's own lower end is 0). Simulator: 27 of 27 points for both arms, worst miss 9 mm. The agent instructions in `agent.py` describe the shelf, the folded home, going to ready first and arms without a gripper. 48 tests pass. Needs a server restart to take effect; waiting for the operator.

* 21:06 restart made the camera swap, planner fix and joint limits live. The operator had rewired the right arm's wrist: motors 1, 2, 3, 4, 5 and 7 answer, 6 does not (joint 5 cabled straight to the gripper), so the right gripper motor and its cable are good and the fault is at joint 6 or its two cables. The server will not run an arm missing joint 6. At the operator's request the left arm's home is now the same pose as the right arm's saved home, [1.38, 0.02, -0.09, -0.62, -7.08, -6.2], in `poses.json` and `layout.json`; this includes the left wrist roll, which read about -155 deg at the time.

* About 21:15 to 21:25: at the operator's request the left arm was moved to home by the agent (9 s, no fault); with the right arm's wrist value the left wrist camera faced the ceiling, so the left wrist is half a turn from the right one. The operator re-posed the left arm and asked twice for its current pose to be saved as its home (`poses.json` and `layout.json`). The agent powered the right arm on at the operator's request; closing its gripper was refused with "Gripper motor cut out: overcurrent" at no load, the same fault seen at 20:53 and 21:05.

* Record tab, at the operator's request: a "by hand" tick box next to Record motion, on by default. Starting a recording with it on switches the chosen arms' motors off (after a confirm that says they will drop unless resting or held), and stopping switches them off again. With it off the arms stay as they are, for recording teleop. Page only change (`index.html`, `app.js`), no restart. Checked on the real dashboard page with the power and record calls intercepted, so nothing reached the robot; the stop path was not exercised.
