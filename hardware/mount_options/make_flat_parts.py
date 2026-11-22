import sys
from pathlib import Path
import numpy as np
import trimesh
from manifold3d import CrossSection, Manifold

OUT = Path(__file__).resolve().parent / "stl"

WIDTH = 64.0
FLANGE = 8.0
LEG_OUT = 44.0
LEG_DOWN = 50.0
GUSSET_T = 8.0
BOLT_D = 8.6
SHELF_BOLT_OUT = 27.0
RAIL_BOLT_ALONG = 10.0
RAIL_BOLT_DOWN = 30.0


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def cyl_y(r, y0, y1, x, z, seg=64):
    return Manifold.cylinder(y1 - y0, r, r, seg).rotate([-90, 0, 0]).translate([x, y0, z])


def cyl_z(r, z0, z1, x, y, seg=64):
    return Manifold.cylinder(z1 - z0, r, r, seg).translate([x, y, z0])


def corner():
    half = WIDTH / 2
    body = box(-half, half, 0, LEG_OUT, -FLANGE, 0)
    body += box(-half, half, 0, FLANGE, -LEG_DOWN, 0)
    fin = CrossSection([[(FLANGE, -FLANGE), (FLANGE, -LEG_DOWN), (LEG_OUT, -FLANGE)]])
    for x in (-half, half - GUSSET_T):
        body += Manifold.extrude(fin, GUSSET_T).rotate([90, 0, 90]).translate([x, 0, 0])
    body -= cyl_z(BOLT_D / 2, -FLANGE - 1, 1, 0, SHELF_BOLT_OUT)
    body -= cyl_y(BOLT_D / 2, -1, FLANGE + 1, RAIL_BOLT_ALONG, -RAIL_BOLT_DOWN)
    return body


def save(manifold, name, turn):
    turned = manifold.rotate(list(turn))
    low, high = turned.bounding_box()[:3], turned.bounding_box()[3:]
    placed = turned.translate([-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]])
    mesh = placed.to_mesh()
    tri = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=False)
    if not tri.is_watertight:
        raise SystemExit(f"{name} is not watertight")
    OUT.mkdir(parents=True, exist_ok=True)
    tri.export(OUT / f"{name}.stl")
    size = tri.extents
    print(f"{name}: {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm, {tri.volume / 1000:.0f} cm3")
    return tri


def main():
    save(corner(), "flat_corner_bracket_print_4", (180, 0, 0))


if __name__ == "__main__":
    sys.exit(main())
