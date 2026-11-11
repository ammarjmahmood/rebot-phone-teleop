import math
import sys
from pathlib import Path
import numpy as np
import trimesh
from manifold3d import CrossSection, Manifold

OUT = Path(__file__).resolve().parent / "stl"
SEG = 96

SQUARE_X = (-39.0, 39.6)
SQUARE_Y = (-40.6, 42.5)
DRUM_Z = (1.5, 11.0)
GROOVE_R = 68.5
FLANGE_R = 72.0
BOLT_X = 52.0
ANCHOR_DEG = (75, 90, 105, -75, -90, -105)

POST = 40.0
POST_GAP = 0.6
COLLAR_WALL = 6.0
COLLAR_H = 42.0
CABLE_Y = 101.0
CABLE_X = 69.6
SHEAVE_R = 22.0
SHEAVE_PITCH = 18.8
SHEAVE_W = 10.0
AXLE_Z = 13.0


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def cyl(r, z0, z1, x=0.0, y=0.0, seg=SEG):
    return Manifold.cylinder(z1 - z0, r, r, seg).translate([x, y, z0])


def cyl_y(r, y0, y1, x=0.0, z=0.0, seg=48):
    return Manifold.cylinder(y1 - y0, r, r, seg).rotate([-90, 0, 0]).translate([x, y0, z])


def cyl_x(r, x0, x1, y=0.0, z=0.0, seg=48):
    return Manifold.cylinder(x1 - x0, r, r, seg).rotate([0, 90, 0]).translate([x0, y, z])


def hexagon(across_flats, z0, z1, x, y, rotate=0.0):
    r = across_flats / math.sqrt(3)
    pts = [(r * math.cos(math.radians(60 * i + rotate)), r * math.sin(math.radians(60 * i + rotate))) for i in range(6)]
    return Manifold.extrude(CrossSection([pts]), z1 - z0).translate([x, y, z0])


def drum():
    z0, z1 = DRUM_Z
    zc = (z0 + z1) / 2
    body = cyl(FLANGE_R, z0, z1)
    reach = FLANGE_R + 1 - GROOVE_R
    body -= Manifold.revolve(CrossSection([[(GROOVE_R, zc), (FLANGE_R + 1, zc - reach), (FLANGE_R + 1, zc + reach)]]), SEG)
    body -= box(SQUARE_X[0], SQUARE_X[1], SQUARE_Y[0], SQUARE_Y[1], z0 - 1, z1 + 1)
    for cx in (SQUARE_X[0], SQUARE_X[1]):
        for cy in (SQUARE_Y[0], SQUARE_Y[1]):
            body -= cyl(1.6, z0 - 1, z1 + 1, cx, cy, 24)
    for x in (-BOLT_X, BOLT_X):
        body -= cyl_y(2.25, -34, 34, x, zc)
        body -= box(x - 4.2, x + 4.2, 16, 30, zc - 4.2, z1 + 1)
        body -= box(x - 3.6, x + 3.6, -21.5, -17.5, zc - 4.2, z1 + 1)
    for deg in ANCHOR_DEG:
        a = math.radians(deg)
        hole = cyl_x(1.3, 46, FLANGE_R + 1, 0, zc, 24)
        pocket = box(47, 58, -3.2, 3.2, zc - 3.2, z1 + 1)
        tool = (hole + pocket).rotate([0, 0, deg])
        body -= tool
    split = (SQUARE_Y[0] + SQUARE_Y[1]) / 2
    upper = body ^ box(-100, 100, split, 100, -10, 30)
    lower = body ^ box(-100, 100, -100, split, -10, 30)
    return upper, lower


def sheave(bore=11.05):
    body = cyl(SHEAVE_R, 0, SHEAVE_W)
    groove = Manifold.revolve(CrossSection([[(SHEAVE_PITCH - 1.0, SHEAVE_W / 2), (SHEAVE_R + 1, SHEAVE_W / 2 - 4.0), (SHEAVE_R + 1, SHEAVE_W / 2 + 4.0)]]), SEG)
    body -= groove
    body -= cyl(bore, -1, SHEAVE_W + 1)
    return body


def collar(z0, z1):
    half = POST / 2 + POST_GAP / 2
    outer = half + COLLAR_WALL
    back = box(-outer, 0, -outer, outer, z0, z1) - box(-half, half, -half, half, z0 - 1, z1 + 1)
    front = box(0, outer, -outer, outer, z0, z1) - box(-half, half, -half, half, z0 - 1, z1 + 1)
    back += box(-6, 0, -outer - 12, outer + 12, z0, z1)
    front += box(0, 6, -outer - 12, outer + 12, z0, z1)
    holes = Manifold()
    for y in (-(outer + 6), outer + 6):
        for z in (z0 + 0.27 * (z1 - z0), z0 + 0.73 * (z1 - z0)):
            holes += cyl_x(2.75, -20, 20, y, z)
            holes += hexagon(8.2, 0, 5, 0, 0).rotate([0, 90, 0]).translate([2.0, y, z])
    back -= holes
    front -= holes
    return back, front


def pulley_bracket():
    z0, z1 = -COLLAR_H, 0.0
    back, front = collar(z0, z1)
    px = -(CABLE_X + SHEAVE_PITCH)
    pz = z0 + AXLE_Z
    cy = -CABLE_Y
    inner = (cy + SHEAVE_W / 2 + 1.0, cy + SHEAVE_W / 2 + 7.0)
    outer = (cy - SHEAVE_W / 2 - 7.0, cy - SHEAVE_W / 2 - 1.0)
    half = POST / 2 + POST_GAP / 2 + COLLAR_WALL
    arm = Manifold.batch_hull([box(-half, -half + 10, -half, -half + 8, z0, z1), box(px - 5, px + 5, inner[0], inner[1] + 2, z0, z1)])
    fork = box(px - 12, px + 12, outer[0], inner[1], z0, z1)
    body = back + arm + fork
    body -= box(px - SHEAVE_R - 3, px + SHEAVE_R + 3, cy - SHEAVE_W / 2 - 1.0, cy + SHEAVE_W / 2 + 1.0, z0 - 1, z1 - 6)
    body -= cyl_y(4.2, cy - 30, cy + 30, px, pz)
    gap = POST / 2 + POST_GAP / 2
    body -= box(-gap, gap, -gap, gap, z0 - 1, z1 + 1)
    return body, front, (px, cy, pz)


def spring_anchor():
    z0, z1 = 0.0, 30.0
    back, front = collar(z0, z1)
    ax = -(CABLE_X + 2 * SHEAVE_PITCH)
    ay = -CABLE_Y
    half = POST / 2 + POST_GAP / 2 + COLLAR_WALL
    arm = Manifold.batch_hull([box(-half, -half + 10, -half, -half + 8, z0, z1), cyl(10, z0, z1, ax, ay, 48)])
    body = back + arm
    body -= cyl(3.4, z0 - 1, z1 + 1, ax, ay, 32)
    gap = POST / 2 + POST_GAP / 2
    body -= box(-gap, gap, -gap, gap, z0 - 1, z1 + 1)
    return body, front, (ax, ay)


def mirrored(manifold):
    return manifold.mirror([0, 1, 0])


def save(manifold, name):
    mesh = manifold.to_mesh()
    tri = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=False)
    if not tri.is_watertight:
        raise SystemExit(f"{name} is not watertight")
    OUT.mkdir(parents=True, exist_ok=True)
    tri.export(OUT / f"{name}.stl")
    size = tri.extents
    print(f"{name}: {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm, {tri.volume / 1000:.0f} cm3")
    return tri


def main():
    upper, lower = drum()
    save(upper, "drum_half_a")
    save(lower, "drum_half_b")
    save(sheave(), "pulley_sheave_608")
    save(sheave(4.3), "pulley_sheave_no_bearing")
    bracket = pulley_bracket()[0]
    anchor = spring_anchor()[0]
    save(bracket, "top_bracket_right")
    save(mirrored(bracket), "top_bracket_left")
    save(anchor, "spring_anchor_right")
    save(mirrored(anchor), "spring_anchor_left")
    save(collar(-COLLAR_H, 0.0)[1], "top_clamp_plate")
    save(collar(0.0, 30.0)[1], "anchor_clamp_plate")


if __name__ == "__main__":
    sys.exit(main())
