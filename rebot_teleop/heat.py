import math
import numpy as np
from rebot_teleop import config as C

INF = math.inf
TABLES = {
    "rs-06": {
        "still": ((8.0, INF), (11.0, 200.0), (17.0, 15.0), (20.0, 11.0), (25.0, 5.0), (30.0, 1.0), (36.0, 1.0)),
        "moving": ((11.0, INF), (17.0, 200.0), (20.0, 36.0), (25.0, 18.0), (30.0, 8.0), (36.0, 4.0)),
    },
    "rs-03": {
        "still": ((15.0, INF), (20.0, 30.0), (30.0, 7.0), (40.0, 1.5), (60.0, 1.5)),
        "moving": ((20.0, INF), (30.0, 189.0), (40.0, 26.0), (50.0, 12.0), (60.0, 7.0)),
    },
    "rs-00": {
        "still": ((5.0, INF), (6.0, 74.0), (7.0, 43.0), (8.0, 16.0), (9.0, 9.0), (10.0, 5.0), (11.0, 3.0), (12.0, 2.0), (13.0, 1.5), (14.0, 1.5)),
        "moving": ((5.0, INF), (6.0, 280.0), (7.0, 120.0), (8.0, 55.0), (9.0, 22.0), (10.0, 18.0), (11.0, 12.0), (12.0, 10.0), (13.0, 6.0), (14.0, 5.0)),
    },
}


def continuous(model):
    return TABLES[model]["still"][0][0]


def seconds_at(table, torque):
    torque = abs(float(torque))
    base = table[0][0]
    if torque <= base:
        return INF
    first_t, first_s = table[1]
    if torque <= first_t:
        energy = first_s * (first_t ** 2 - base ** 2)
        return energy / (torque ** 2 - base ** 2)
    for (t0, s0), (t1, s1) in zip(table[1:], table[2:]):
        if torque <= t1:
            u = (torque - t0) / (t1 - t0)
            return math.exp(math.log(s0) + u * (math.log(s1) - math.log(s0)))
    last_t, last_s = table[-1]
    return last_s * (last_t / torque) ** 2


def heat_rate(model, torque, speed_deg_s):
    moving = min(abs(float(speed_deg_s)) / C.HEAT_MOVING_DEG_S, 1.0)
    rate = (1.0 - moving) / seconds_at(TABLES[model]["still"], torque) + moving / seconds_at(TABLES[model]["moving"], torque)
    if rate > 0.0:
        return rate
    return -max(0.0, 1.0 - (float(torque) / continuous(model)) ** 2) / C.HEAT_COOL_S


class Budget:
    def __init__(self, models):
        self.models = list(models)
        self.used = np.zeros(len(self.models))
        self.rates = np.zeros(len(self.models))

    def copy(self):
        other = Budget(self.models)
        other.used = self.used.copy()
        other.rates = self.rates.copy()
        return other

    def rates_for(self, torques, speeds):
        return np.array([heat_rate(m, t, s) for m, t, s in zip(self.models, torques, speeds)])

    def step(self, torques, speeds, dt):
        self.rates = self.rates_for(torques, speeds)
        self.used = np.clip(self.used + self.rates * dt, 0.0, 1.5)
        return self.used

    def cool(self, seconds):
        if seconds > 0:
            self.used = np.clip(self.used - seconds / C.HEAT_COOL_S, 0.0, 1.5)

    def seconds_left(self, limit=None):
        limit = C.HEAT_LOWER if limit is None else limit
        return [max(0.0, (limit - u) / r) if r > 0 else None for u, r in zip(self.used, self.rates)]

    def summary(self):
        left = self.seconds_left()
        heating = [i for i, s in enumerate(left) if s is not None]
        if heating:
            worst = min(heating, key=lambda i: left[i])
        else:
            worst = int(np.argmax(self.used))
        pct = round(float(self.used[worst]) * 100)
        state = "lowering" if self.used[worst] >= C.HEAT_LOWER and left[worst] is not None else "warm" if self.used[worst] >= C.HEAT_WARN else "ok"
        if not heating and self.used[worst] > 0.02:
            state = "cooling"
        return {
            "joint": worst + 1, "pct": pct, "seconds": None if left[worst] is None else round(left[worst]), "state": state,
            "pct_all": [round(float(u) * 100) for u in self.used], "seconds_all": [None if s is None else round(s) for s in left],
        }


def motor_list(overrides=None):
    motors = list(C.MOTORS)
    for key, model in (overrides or {}).items():
        index = int(key) - 1
        if 0 <= index < 6 and model in TABLES and model in C.PEAK_NM:
            motors[index] = (motors[index][0], model)
    return motors


def motor_models(overrides=None):
    return [model for _, model in motor_list(overrides)[:6]]
