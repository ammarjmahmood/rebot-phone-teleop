import math
import sys
from pathlib import Path
import numpy as np
import trimesh
from manifold3d import Manifold

OUT = Path(__file__).resolve().parent / "stl"

POST_W = 40.0
POST_D = 60.0
RAIL_T = 38.0
RAIL_H = 140.0
SLOT_ALONG = 85.0
SLOT_ACROSS = 45.0
PLATE_T = 10.0
BOLT_D = 8.8
NUT_FLATS = 13.4
NUT_T = 8.6
NUT_DEPTH = 18.0
LEG = 115.0
TIP_CUT = 10.0
BLOCK_X = (-44.0, 42.0)
BLOCK_BOLT_X = 18.0
BLOCK_ROD_X = -18.0
ROD_SEAT = 30.0
WASHER_D = 36.0
SHOULDER_X = 175.0
ROD_FROM_CENTRE = SLOT_ALONG - (BLOCK_BOLT_X - BLOCK_ROD_X)
HEAD_X = 240.0
HEAD_FOOT_X = 60.0
HEAD_FOOT_Z = (-40.0, 70.0)
HEAD_OWN_BOLT = (10.0, 45.0)
HEAD_CAP = 6.0
HEAD_POST_H = 100.0
POST_BOLT = 34.0
GUIDE_T = 15.0
GUIDE_LIP = 12.0


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def cyl_y(r, y0, y1, x=0.0, z=0.0, seg=64):
    return Manifold.cylinder(y1 - y0, r, r, seg).rotate([-90, 0, 0]).translate([x, y0, z])


def cyl_z(r, z0, z1, x=0.0, y=0.0, seg=64):
    return Manifold.cylinder(z1 - z0, r, r, seg).translate([x, y, z0])


def on_slope(part):
    return part.rotate([45, 0, 0]).translate([0, LEG / 2, 0])


def block():
    x0, x1 = BLOCK_X
    body = box(x0, x1, 0, LEG, -LEG / 2, LEG / 2)
    body -= on_slope(box(x0 - 1, x1 + 1, -LEG, LEG, -LEG, 0))
    body -= box(x0 - 1, x1 + 1, -1, LEG + 1, -LEG, -LEG / 2 + TIP_CUT)
    body -= box(x0 - 1, x1 + 1, LEG - TIP_CUT, LEG + 1, -LEG, LEG)
    corner = NUT_FLATS / math.sqrt(3)
    x = BLOCK_BOLT_X
    for s in (-SLOT_ACROSS, SLOT_ACROSS):
        body -= on_slope(cyl_z(BOLT_D / 2, -1, LEG, x, s))
        if s > 0:
            slot = box(x - NUT_FLATS / 2, x + NUT_FLATS / 2, s - corner, s + LEG, NUT_DEPTH, NUT_DEPTH + NUT_T)
        else:
            slot = box(x - NUT_FLATS / 2, x + NUT_FLATS / 2, s - LEG, s + corner, NUT_DEPTH, NUT_DEPTH + NUT_T)
        body -= on_slope(slot)
    body -= cyl_y(BOLT_D / 2, -1, LEG + 1, BLOCK_ROD_X, 0)
    body -= cyl_y(WASHER_D / 2, ROD_SEAT, LEG + 1, BLOCK_ROD_X, 0)
    return body


def head_post():
    half = HEAD_FOOT_X / 2
    z0, z1 = HEAD_FOOT_Z
    body = box(-half, half, 0, POST_W, z0, z1)
    body += box(-half, half, -RAIL_T, POST_W + RAIL_T, z1, z1 + HEAD_CAP)
    body += box(-POST_D / 2, POST_D / 2, 0, POST_W, z1, z1 + HEAD_CAP + HEAD_POST_H)
    body -= cyl_y(BOLT_D / 2, -1, POST_W + 1, SHOULDER_X + ROD_FROM_CENTRE - HEAD_X, 0)
    body -= cyl_y(BOLT_D / 2, -1, POST_W + 1, HEAD_OWN_BOLT[0], HEAD_OWN_BOLT[1])
    return body


def drill_guide():
    x0, x1 = -ROD_FROM_CENTRE - 20, HEAD_X + HEAD_OWN_BOLT[0] - SHOULDER_X + 20
    body = box(x0, x1, 0, GUIDE_T, -RAIL_H / 2, RAIL_H / 2)
    body += box(x0, x1, -GUIDE_LIP, GUIDE_T, RAIL_H / 2, RAIL_H / 2 + 6)
    for x in (-ROD_FROM_CENTRE, ROD_FROM_CENTRE):
        body -= cyl_y(BOLT_D / 2, -1, GUIDE_T + 1, x, 0)
    body -= cyl_y(BOLT_D / 2, -1, GUIDE_T + 1, HEAD_X + HEAD_OWN_BOLT[0] - SHOULDER_X, HEAD_OWN_BOLT[1])
    for x in (-POST_BOLT, POST_BOLT):
        for z in (-POST_BOLT, POST_BOLT):
            body -= cyl_y(BOLT_D / 2, -1, GUIDE_T + 1, x, z)
    body -= box(-1.0, 1.0, GUIDE_T - 1.0, GUIDE_T + 1, -RAIL_H / 2 - 1, RAIL_H / 2 + 7)
    return body


def on_bed(manifold, turn):
    turned = manifold.rotate(turn)
    low, high = turned.bounding_box()[:3], turned.bounding_box()[3:]
    return turned.translate([-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]])


def save(manifold, name, turn=(0, 0, 0)):
    mesh = on_bed(manifold, list(turn)).to_mesh()
    tri = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=False)
    if not tri.is_watertight:
        raise SystemExit(f"{name} is not watertight")
    OUT.mkdir(parents=True, exist_ok=True)
    tri.export(OUT / f"{name}.stl")
    size = tri.extents
    print(f"{name}: {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm, {tri.volume / 1000:.0f} cm3")
    return tri


def main():
    part = block()
    save(part, "block_a_print_twice", (0, -90, 0))
    save(part.mirror([1, 0, 0]), "block_b_print_twice", (0, -90, 0))
    save(head_post(), "head_post", (0, -90, 0))
    save(drill_guide(), "drill_guide", (-90, 0, 0))


if __name__ == "__main__":
    sys.exit(main())
