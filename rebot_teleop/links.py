import ipaddress
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
import psutil
from rebot_teleop import config as C

SKIP = ("docker", "br-", "veth", "virbr", "lo", "l4tbr")
LEGACY_GIST = Path.home() / "robot-mia" / ".link_gist_id"


def addresses():
    lan, tailnet = [], []
    for name, entries in psutil.net_if_addrs().items():
        if name.startswith(SKIP):
            continue
        for entry in entries:
            if entry.family.name != "AF_INET":
                continue
            address = ipaddress.ip_address(entry.address)
            if address.is_loopback or address.is_link_local:
                continue
            (tailnet if address in ipaddress.ip_network("100.64.0.0/10") else lan).append(entry.address)
    return lan, tailnet


def urls():
    lan, tailnet = addresses()
    out = []
    for kind, items in (("WiFi", lan), ("Tailscale", tailnet)):
        for address in items:
            out.append({"kind": kind, "dashboard": f"http://{address}:{C.HTTP_PORT}/", "quest": f"https://{address}:{C.HTTPS_PORT}/quest", "phone": f"https://{address}:{C.HTTPS_PORT}/phone"})
    return out


def markdown(items):
    lines = ["# reBot live teleop link", ""]
    for index, item in enumerate(items):
        title = ">> LOCAL, FASTEST <<" if index == 0 and item["kind"] == "WiFi" else item["kind"]
        lines += [f"## [{title}]({item['quest']})", "", f"`{item['quest']}`", "", f"Phone: [{item['phone']}]({item['phone']})  ", f"Dashboard: [{item['dashboard']}]({item['dashboard']})", ""]
    lines += [f"_Updated {time.strftime('%A %b %d, %Y %I:%M %p')}_", "", "Open the Quest link in the Quest browser and press Enter VR.", "The first visit asks to trust the robot's certificate: Advanced, then Proceed."]
    return "\n".join(lines) + "\n"


class Gist:
    def __init__(self):
        self.status = "off"
        self.url = None
        self.last = None
        self.gh = shutil.which("gh")

    def gist_id(self):
        saved = C.load("gist.json", {}).get("id")
        if saved:
            return saved
        if LEGACY_GIST.exists():
            legacy = LEGACY_GIST.read_text().strip()
            if legacy:
                C.save("gist.json", {"id": legacy})
                return legacy
        return None

    def publish(self):
        if not self.gh:
            self.status = "gh is not installed"
            return
        items = urls()
        if not items:
            self.status = "no network"
            return
        key = json.dumps(items)
        if key == self.last:
            return
        content = markdown(items)
        gist = self.gist_id()
        try:
            if gist:
                files = json.loads(subprocess.run([self.gh, "api", f"/gists/{gist}", "-q", ".files|keys"], capture_output=True, text=True, timeout=20, check=True).stdout)
                name = files[0] if files else "rebot-link.md"
                body = json.dumps({"description": "reBot live teleop link", "files": {name: {"content": content}}})
                subprocess.run([self.gh, "api", "-X", "PATCH", f"/gists/{gist}", "--input", "-"], input=body, capture_output=True, text=True, timeout=20, check=True)
            else:
                result = subprocess.run([self.gh, "gist", "create", "--desc", "reBot live teleop link", "--filename", "rebot-link.md", "-"], input=content, capture_output=True, text=True, timeout=30, check=True)
                gist = result.stdout.strip().rstrip("/").split("/")[-1]
                C.save("gist.json", {"id": gist})
            self.url = f"https://gist.github.com/{gist}"
            self.status = "updated " + time.strftime("%H:%M")
            self.last = key
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            self.status = f"gist update failed: {getattr(error, 'stderr', '') or error}".strip()[:200]

    def run_forever(self):
        def loop():
            while True:
                self.publish()
                time.sleep(120)
        threading.Thread(target=loop, name="gist", daemon=True).start()
