import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from dotenv import dotenv_values
from rebot_teleop.models import Principal


class Auth:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        self.values = dict(dotenv_values(path))
        for key in ["REBOT_TELEOP_SESSION_SECRET", "REBOT_TELEOP_ADMIN_TOKEN"]:
            if not self.values.get(key):
                self.save(key, secrets.token_urlsafe(32))
        self.attempts = {}

    def save(self, key: str, value: str):
        self.values[key] = value
        lines = self.path.read_text().splitlines()
        lines = [line for line in lines if not line.startswith(key + "=")]
        lines.append(key + "=" + json.dumps(value))
        self.path.write_text("\n".join(lines) + "\n")
        self.path.chmod(0o600)

    def sign(self, principal: Principal):
        value = base64.urlsafe_b64encode(json.dumps({"id": principal.id, "role": principal.role, "exp": time.time() + 28800}).encode()).decode()
        signature = hmac.new(self.values["REBOT_TELEOP_SESSION_SECRET"].encode(), value.encode(), hashlib.sha256).hexdigest()
        return value + "." + signature

    def resolve(self, cookie: str, local: bool):
        try:
            value, signature = cookie.split(".")
            expected = hmac.new(self.values["REBOT_TELEOP_SESSION_SECRET"].encode(), value.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            data = json.loads(base64.urlsafe_b64decode(value))
            if data["exp"] < time.time():
                return None
            return Principal(id=data["id"], role=data["role"], local=local)
        except (ValueError, KeyError, TypeError):
            return None

    def login(self, token, address):
        token = token.strip()
        if not token:
            raise ValueError("Enter the pairing code shown on the computer")
        cutoff = time.monotonic() - 60
        attempts = [t for t in self.attempts.get(address, []) if t > cutoff]
        if len(attempts) >= 5:
            raise ValueError("Too many pairing attempts; wait one minute")
        if hmac.compare_digest(token, self.values["REBOT_TELEOP_ADMIN_TOKEN"]):
            self.attempts.pop(address, None)
            return Principal(id=secrets.token_hex(12), role="owner")
        pair = json.loads(self.values.get("REBOT_TELEOP_PAIRING", "{}"))
        if pair.get("expires", 0) > time.time() and hmac.compare_digest(token, pair.get("code", "")):
            self.save("REBOT_TELEOP_PAIRING", "{}")
            self.attempts.pop(address, None)
            return Principal(id=secrets.token_hex(12), role=pair["role"])
        self.attempts[address] = attempts + [time.monotonic()]
        raise ValueError("Pairing code is invalid or expired")

    def pair(self, role):
        if role not in {"viewer", "operator"}:
            raise ValueError("Choose viewer or operator")
        code = str(secrets.randbelow(90000000) + 10000000)
        self.save("REBOT_TELEOP_PAIRING", json.dumps({"code": code, "role": role, "expires": time.time() + 300}))
        self.attempts.clear()
        return code
