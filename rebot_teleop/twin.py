import hashlib
import json
import math
from pathlib import Path
import numpy as np
import pinocchio as pin
from rebot_teleop import config as C
from rebot_teleop.kin import visuals

CACHE = C.DATA / "mesh_cache"
MESH_ROOTS = (C.ROOT / "meshes", Path.home() / "rebot-phone-teleop" / "meshes")
STL_DTYPE = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def read_stl(path):
    raw = Path(path).read_bytes()
    if raw[:5].lower() == b"solid" and b"facet" in raw[:400]:
        vertices = []
        for line in raw.decode(errors="ignore").splitlines():
            parts = line.split()
            if parts and parts[0] == "vertex":
                vertices.append([float(v) for v in parts[1:4]])
        return np.asarray(vertices, dtype=np.float32).reshape(-1, 3, 3)
    count = int(np.frombuffer(raw[80:84], dtype="<u4")[0])
    return np.frombuffer(raw[84:84 + count * 50], dtype=STL_DTYPE)["v"].copy()


def decimate(triangles, cells=90):
    if len(triangles) < 4000:
        return triangles
    points = triangles.reshape(-1, 3)
    low, high = points.min(axis=0), points.max(axis=0)
    size = max(float(np.max(high - low)) / cells, 1e-9)
    keys = np.floor((points - low) / size).astype(np.int64)
    _, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    sums = np.zeros((inverse.max() + 1, 3))
    np.add.at(sums, inverse, points)
    counts = np.bincount(inverse)[:, None]
    centers = (sums / counts).astype(np.float32)
    faces = inverse.reshape(-1, 3)
    good = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    return centers[faces[good]]


def write_stl(triangles):
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = normals / np.where(lengths == 0, 1, lengths)
    out = np.zeros(len(triangles), dtype=STL_DTYPE)
    out["normal"] = normals
    out["v"] = triangles
    return b"\0" * 80 + np.uint32(len(triangles)).tobytes() + out.tobytes()


def resolve(file):
    if file.startswith("../meshes/"):
        rest = file[len("../meshes/"):]
        for root in MESH_ROOTS:
            if (root / rest).exists():
                return root / rest
        return None
    path = C.ASSETS / file
    return path if path.exists() else None


def mesh_bytes(key):
    path = CACHE / f"{key}.stl"
    return path.read_bytes() if path.exists() else None


def build_mesh(source, scale):
    digest = hashlib.sha1(f"{source}:{scale}:{source.stat().st_mtime}".encode()).hexdigest()[:16]
    target = CACHE / f"{digest}.stl"
    if not target.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        triangles = read_stl(source) * np.asarray(scale, dtype=np.float32)
        target.write_bytes(write_stl(decimate(triangles)))
    return digest


def origin_matrix(xyz, rpy):
    m = np.eye(4)
    m[:3, :3] = pin.rpy.rpyToMatrix(*rpy)
    m[:3, 3] = xyz
    return np.round(m, 6).tolist()


def scene():
    parts = []
    for item in visuals():
        source = resolve(item["file"])
        if source is None:
            continue
        key = build_mesh(source, item["scale"])
        parts.append({"link": item["link"], "mesh": key, "origin": origin_matrix(item["xyz"], item["rpy"]), "rgba": item["rgba"]})
    body = []
    colors_file = C.ASSETS / "robot_body" / "colors.json"
    colors = json.loads(colors_file.read_text()) if colors_file.exists() else {}
    turn = origin_matrix([0.0, 0.0, C.POST_TOP_M], [0.0, 0.0, math.pi / 2])
    for name, rgb in colors.items():
        source = C.ASSETS / "robot_body" / f"{name}.stl"
        if source.exists():
            body.append({"mesh": build_mesh(source, [0.001] * 3), "origin": turn, "rgba": [*rgb, 1.0]})
    post = {"size": [0.06, C.POST_W_M, C.POST_TOP_M], "center": [0.0, 0.0, C.POST_TOP_M / 2]}
    return {"arm_parts": parts, "body": body, "post": post, "mounts": C.MOUNTS}
