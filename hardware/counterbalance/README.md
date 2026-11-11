# Joint 1 counterbalance

The arms hang off the side of the post, so joint 1 holds the whole arm up whenever it reaches forward. Its RS06 can hold 8 Nm while still, and the ready pose alone asks 7.5 Nm. This kit takes part of that load off the motor. A printed drum clamps onto the shoulder bracket, a steel cable wraps on it, runs up over a pulley at the top of the post and down behind the arm to a hanging weight (recommended) or a spring.

![Right arm at the ready pose](assembly_ready.png)

## Which version

| | Joint 1 at ready | Forward poses above 8 Nm | Joint 1 at home (hanging) |
|---|---|---|---|
| nothing | 7.5 Nm | about 40% | 0.1 Nm |
| hanging weight, 15 lb (6.8 kg) | about 3 Nm | about 10% | about 4.6 Nm |
| spring, 0.62 N/mm | 4.3 Nm | about 15 to 18% | 0.1 Nm |

All from the arm model, not measured yet. The weight wins: it pulls the same at every angle, which happens to match how the arm is used, it needs no spring hunting, and you tune it by adding or removing plates. Its costs: joint 1 holds about 4.6 Nm at home instead of nothing (still under its 8 Nm), and when an arm is powered off the weight swings it back about 25 degrees until gravity balances it, so support the arm before Power off.

## Check before printing

These numbers come from the reBot meshes and `rebot_teleop/config.py`. Measure them on the robot first; all of them are at the top of `make_parts.py`.

1. The post is 40 x 40 mm (`POST`).
2. The square base of the shoulder bracket (the U shaped part that joint 1 turns, right above the joint 1 motor) is 78 x 82.5 mm across its bottom 10 mm (`SQUARE_X`, `SQUARE_Y`).
3. There is at least 1.5 mm between the bottom of that bracket and the face of the joint 1 motor (`DRUM_Z`).
4. There are at least 42 mm of post between the top edge of the arm base plate and the top of the post (`COLLAR_H`), and nothing on the post top is in the way of a clamp.

To regenerate after changing a number: `pip install manifold3d trimesh` then `python make_parts.py`.

## Print (Bambu P1S, PETG, both arms in two plates)

Everything for both arms does not fit on one 256 mm plate: the two drums and the two brackets alone are more area than the bed. `print/final/counterbalance_both_arms_PETG_P1S.3mf` is one project with three plates, about 9.3 hours in total:

| Plate | Parts | Time |
|---|---|---|
| 1 | right and left top brackets, two clamp plates, two pulley wheels | 5.6 h |
| 2 | drum for one arm (both halves) | 1.8 h |
| 3 | drum for the other arm (both halves) | 1.8 h |

Settings on every plate: Bambu PETG Basic, 0.24 mm layers, 4 walls, 25% gyroid, no brim, no support, textured PEI plate. The pulley wheel is the bearing free version that turns on the M8 bolt; `pulley_sheave_608.stl` takes a 608 skateboard bearing if you have one. The spring version also needs `spring_anchor_right.stl`, `spring_anchor_left.stl` and two `anchor_clamp_plate.stl`.

![Parts](parts.png)

## Shopping list

Both arms, hanging weight version. Store SKUs are from homedepot.ca where one turned up; the others are standard items in the same aisles.

| Item | Qty | Where |
|---|---|---|
| 1/16 in galvanized steel aircraft cable, 7x7 | 3 m | Home Depot rope counter by the foot, or [KingCord 1/16 in aircraft cable](https://homedepot.ca/product/kingcord-1-16-inch-x-500-ft-galvanised-steel-7-x-7-aircraft-cable-95-lb-safe-working-load/1001857133) |
| [Everbilt 1/16 in aluminium cable stops](https://homedepot.ca/product/everbilt-1-16-inch-aluminum-cable-stops-20-piece/1001026392) | 1 pack | Home Depot, SKU 1001026392 |
| 1/16 in aluminium ferrules (sleeves) | 2 | Home Depot, Everbilt, same aisle as the stops |
| [Everbilt carabiner clip set](https://www.homedepot.ca/product/everbilt-eb-link-hook-carabiner-clip-set/1001846154) | 1 | Home Depot, SKU 1001846154, to hang the weights |
| [Paulin M5-0.8 x 20 mm hex cap screw](https://www.homedepot.ca/product/paulin-m5-80-x-20mm-class-8-8-metric-hex-cap-screw-din-933-zinc-plated/1000128425) and M5 hex nuts | 8 each | Home Depot, Paulin metric drawer |
| Paulin M8-1.25 x 40 mm hex cap screw, M8 nylon lock nut, 2 M8 washers | 2 sets | Home Depot, Paulin metric drawer |
| Paulin M4-0.7 x 30 mm screw and M4 hex nut | 4 each | Home Depot, Paulin metric drawer |
| 1 in cast iron weight plates: one 10 lb and one 5 lb per arm | 2 + 2 | [Canadian Tire, WaKe Athletic cast iron plates](https://www.canadiantire.ca/en/pdp/wa-ke-athletic-1-in-cast-iron-weight-plate-single-assorted-weights-1842013p.html); bricks in a bag or a jug of sand work too |
| a luggage scale | 1 | optional, to check the pull |

For the swaged stops: squeeze them flat in a vise or with a crimper, then hang 20 kg on a test piece before trusting it.

Spring version instead of weights, both arms: 2 extension springs of about 0.62 N/mm (3.5 lbf/in) that can stretch 190 mm, 2 [Everbilt eye and hook turnbuckles](https://homedepot.ca/product/everbilt-5-1-2-inch-zinc-eye-hook-turnbuckle/1000769521), 2 [Everbilt 1/4 x 2 in eye bolts](https://homedepot.ca/product/everbilt-1-4-inch-x-2-inch-eye-bolt-with-hex-nut-zinc-1-pc-/1000769927), and 8 more M5 screws and nuts.

## Assembly

Do this with the arm powered off and supported.

**Step 1. Drum.** Close the two drum halves around the square base of the shoulder bracket, slots facing outward away from the post. Drop an M4 nut in each nut slot, put the M4 x 30 screws through from the other half and tighten until the drum cannot move on the bracket. Turn the arm by hand: the drum must not touch the motor face below it.

![Step 1](steps/step1_drum.png)

**Step 2. Cable end.** With the arm hanging at home, find the three pockets on the drum's back side (towards the post side of the robot, away from where the arm reaches). Crimp a stop on the cable end, drop it in the middle pocket and lead the cable out through the hole into the groove. From there it goes straight up.

![Step 2](steps/step2_anchor.png)

**Step 3. Pulley bracket.** Put the M8 bolt through the bracket's fork with a washer, the pulley wheel, a washer, and the lock nut; snug, so the wheel still spins. Clamp the bracket around the post just under its top with the clamp plate on the front, four M5 x 20 screws with the nuts in their hex pockets. The fork sits out to the arm's side and behind the shoulder, straight above the back of the drum.

![Step 3](steps/step3_bracket.png)

**Step 4. Weight.** Run the cable up into the front of the pulley groove, over the top and down the back. Loop the end through a carabiner with a ferrule, about 30 cm above the floor with the arm hanging. Clip on the 10 lb plate first, power the arm on and check the dashboard (below), then add the 5 lb plate.

![Step 4](steps/step4_weight.png)

Spring version: instead of the weight, clamp the spring anchor lower on the post below the pulley and hang spring, turnbuckle and eye bolt from it. With the arm hanging, take up the turnbuckle until the cable is just taut.

![Step 4, spring](steps/step4_spring.png)

## Tell the software

Add this to `~/.rebot-teleop/arms/right/settings.json` on the Jetson (keep any keys that are already there):

```json
{"counterbalance": {"constant_nm": 4.6, "forward": -1, "j2_min_deg": -20}}
```

and for the left arm, in `~/.rebot-teleop/arms/left/settings.json`:

```json
{"counterbalance": {"constant_nm": 4.6, "forward": 1, "j2_min_deg": -20}}
```

`constant_nm` is the weight in kg times 0.683 (the 69.6 mm drum radius times gravity): 4.6 for 15 lb, 3.1 for the 10 lb plate alone. For the spring version use `{"nm_per_rad": 3.0, "zero_deg": 90, "forward": -1, "max_deg": 170, "j2_min_deg": -20}` on the right and `zero_deg` -90, `forward` 1 on the left.

Restart the `rebot` service with both arms powered off. The gravity model then includes the counterbalance, so the budget timers, the sequence check and the feedforward stay right, and joint 2 is kept above -20 degrees where the upper arm would hit the drum. Check on the Home tab: at the ready pose the measured Nm on joint 1 should sit near the grav column, about 3 Nm, and at home about 4.6 Nm the other way.

## Quick test without printing

To feel the difference before printing: tie 550 paracord twice around the shoulder bracket just above its square base (below the joint 2 motor), lead it up over an Everbilt 1-1/2 in swivel pulley tied to the post top, and hang about 9 kg (four bricks or 20 lb of plates) behind. The square bracket is about 45 mm in radius, so it takes more weight than the drum. Tape the cord so it cannot ride up, support the arm whenever it is powered off, and keep the weight low.

## Things to watch

* All clearances are checked against the arm model only: the drum sits 2 mm above the joint 1 motor and the cable passes 23 mm from the motor housing. Check both by hand before powering on.
* The weight falls if the cable or a stop lets go: keep it within 30 cm of the floor.
* Keep fingers out of the drum and the pulley when the arm moves.
* Look at the cable after the first day. A fraying strand means replace it.

## Swapping joint 1 to a bigger motor later

The motor library on the Jetson already drives the RobStride RS03, and the server takes a per joint motor setting. From the RS03 manual:

| | RS06 (now) | RS03 |
|---|---|---|
| Holds while still | 8 Nm | 15 Nm |
| 20 Nm while still | 11 s | 30 s |
| Peak | 36 Nm | 60 Nm |
| Size | 88 mm across, 49 mm deep | 98 mm across, 54 mm deep |
| Weight | 621 g | 880 g |

It does not bolt in: its housing and output flange have different bolt circles, so it needs two aluminium adapter plates. Set the new motor to CAN ID 1 with host ID 0xFD, set its zero, then add `"motors": {"1": "rs-03"}` to that arm's `settings.json`.
