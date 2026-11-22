import sys, json, math, time, itertools
import numpy as np
sys.path.insert(0, '/home/dev-jetson-orin/rebot-phone-teleop')
import pinocchio as pin
from rebot_teleop import config as C
from rebot_teleop import heat as H
from rebot_teleop.kin import Kinematics

LIM = np.radians([[-158, 158], [0, 180], [0, 178], [-88, 88], [-88, 88], [-179, 179]])
CONT = np.array([8.0, 8.0, 8.0, 5.0, 5.0, 5.0])
TABLE = 0.68

def make(mount, payload=0.0):
    k = Kinematics(mount)
    k.lower, k.upper = LIM[:, 0].copy(), LIM[:, 1].copy()
    if payload:
        f = k.model.frames[k.tip]
        k.model.appendBodyToJoint(f.parentJoint, pin.Inertia(payload, np.zeros(3), np.eye(3) * 1e-5), f.placement)
        k.data = k.model.createData()
    return k

def frame(approach, turn):
    a = np.asarray(approach, float); a /= np.linalg.norm(a)
    ref = np.array([0.0, 1.0, 0.0]) if abs(a[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
    b = np.cross(a, ref); b /= np.linalg.norm(b)
    c = np.cross(a, b)
    b2 = b * math.cos(turn) + c * math.sin(turn)
    return np.column_stack([a, b2, np.cross(a, b2)])

POINTS = [(x, y, z) for x in (0.30, 0.40, 0.50, 0.60, 0.70) for y in (-0.10, -0.25, -0.40) for z in (TABLE + 0.05, TABLE + 0.20)]
APPROACH = {'down': (0, 0, -1), 'forward': (1, 0, -0.35)}
TURNS = [i * math.pi / 4 for i in range(8)]

def valid(k, q):
    L = k.links(q)
    for name in ('link3', 'link4', 'link5', 'link6'):
        x, y, z = L[name][:3, 3]
        if x > 0.22 and z < TABLE + 0.03:
            return False
        if y > 0.03:
            return False
        if abs(x) < 0.05 and abs(y) < 0.05 and name != 'link3':
            return False
    return True

def best(k, g, p, approach, rng, seeds):
    out = None
    for turn in TURNS:
        R = frame(approach, turn)
        for seed in seeds:
            q, err, rerr = k.ik(np.array(p), R, seed, iterations=40)
            if err > 0.01 or rerr > 0.12:
                continue
            if np.any(q < k.lower - 1e-6) or np.any(q > k.upper + 1e-6) or not valid(k, q):
                continue
            tau = np.abs(g(q))
            u = float(np.max(tau / CONT))
            if out is None or u < out[0]:
                out = (u, q, tau)
    return out

def evaluate(mount, payload=0.0, nseed=5, rng_seed=1):
    k = make(mount, payload); g = k.gravity_function()
    rng = np.random.default_rng(rng_seed)
    res = []
    carry = []
    for name, a in APPROACH.items():
        for p in POINTS:
            seeds = [rng.uniform(k.lower, k.upper) for _ in range(nseed)] + carry[-3:]
            b = best(k, g, p, a, rng, seeds)
            if b is None:
                res.append({'p': p, 'approach': name, 'ok': False}); continue
            carry.append(b[1])
            res.append({'p': p, 'approach': name, 'ok': True, 'u': b[0], 'tau': b[2].round(2).tolist(), 'q': np.degrees(b[1]).round(1).tolist(), 'worst': int(np.argmax(b[2] / CONT)) + 1})
    return res

def summary(res):
    ok = [r for r in res if r['ok']]
    n = len(res)
    if not ok:
        return {'reach': 0.0, 'hold': 0.0}
    u = np.array([r['u'] for r in ok])
    tau = np.array([r['tau'] for r in ok])
    worst = np.bincount([r['worst'] for r in ok], minlength=7)[1:]
    return {'reach': len(ok) / n, 'hold': float(np.sum(u <= 1.0)) / n, 'median_u': float(np.median(u)), 'p90_u': float(np.percentile(u, 90)), 'max_u': float(u.max()),
            'j_med': np.median(tau, axis=0).round(1).tolist(), 'j_max': tau.max(axis=0).round(1).tolist(), 'worst_joint_counts': worst.tolist()}

if __name__ == '__main__':
    t = time.time()
    r = evaluate({'xyz': [0.0, -0.02, 1.145], 'rpy_deg': [90.0, 0.0, 0.0]})
    print(json.dumps(summary(r)), 'seconds %.1f' % (time.time() - t))
