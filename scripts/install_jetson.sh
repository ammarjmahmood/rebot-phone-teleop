#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi
USER_NAME="${SUDO_USER:-dev-jetson-orin}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
echo "Setting up the reBot on this Jetson for $USER_NAME from $REPO"

cat > /etc/systemd/system/rebot.service <<EOF
[Unit]
Description=reBot server: dashboard, teleop, sequencer, cameras, table lift and robot API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$REPO
Environment=HOME=$HOME_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$REPO/.venv/bin/python -m rebot_teleop
Restart=always
RestartSec=2
Nice=-10
LimitRTPRIO=50
StandardOutput=append:$HOME_DIR/.rebot-teleop/server.log
StandardError=append:$HOME_DIR/.rebot-teleop/server.log

[Install]
WantedBy=multi-user.target
EOF
mkdir -p "$HOME_DIR/.rebot-teleop" && chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.rebot-teleop"
if systemctl list-unit-files | grep -q '^rebot-teleop.service'; then
  systemctl disable --now rebot-teleop.service || true
  rm -f /etc/systemd/system/rebot-teleop.service
  rm -rf /etc/systemd/system/rebot-teleop.service.d
fi
systemctl daemon-reload
systemctl enable --now rebot.service

cat > /etc/udev/rules.d/80-rebot-can.rules <<'EOF'
ACTION=="add", SUBSYSTEM=="net", DRIVERS=="peak_usb", RUN+="/usr/sbin/ip link set $name type can bitrate 1000000 restart-ms 100", RUN+="/usr/sbin/ip link set $name txqueuelen 1000", RUN+="/usr/sbin/ip link set $name up"
EOF
rm -f /etc/udev/rules.d/80-rebot-teleop-can.rules
install -m 644 "$REPO/vendor/angstrong/angstrong-camera.rules" /etc/udev/rules.d/56-angstrong-camera.rules
command -v adb >/dev/null || apt-get install -y adb || true
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2833", MODE="0666", GROUP="plugdev"' > /etc/udev/rules.d/51-rebot-quest.rules
udevadm control --reload
udevadm trigger --subsystem-match=net --action=add || true
for path in /sys/class/net/*; do
  if [ "$(cat "$path/type" 2>/dev/null)" = 280 ]; then
    name="$(basename "$path")"
    ip link set "$name" down || true
    ip link set "$name" type can bitrate 1000000 restart-ms 100 || true
    ip link set "$name" txqueuelen 1000 || true
    ip link set "$name" up || true
  fi
done

if [ -f /etc/gdm3/custom.conf ]; then
  python3 - "$USER_NAME" <<'EOF'
import re, sys
path = "/etc/gdm3/custom.conf"
text = open(path).read()
text = re.sub(r"(?m)^#?\s*AutomaticLoginEnable\s*=.*\n", "", text)
text = re.sub(r"(?m)^#?\s*AutomaticLogin\s*=.*\n", "", text)
text = text.replace("[daemon]\n", f"[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin={sys.argv[1]}\n", 1)
open(path, "w").write(text)
EOF
fi

if [ "${KEEP_ROS:-0}" != "1" ]; then
  if dpkg -l | grep -q '^ii  ros-'; then
    echo "Removing ROS 2 packages"
    apt-get purge -y 'ros-*' || true
    apt-get autoremove -y || true
  fi
  rm -f /etc/apt/sources.list.d/ros2*.list /usr/share/keyrings/ros-archive-keyring.gpg
  rm -rf /opt/ros
fi

echo "Done. The dashboard starts with the Jetson and opens full screen after login (F11 leaves full screen)."
echo "Dashboard: http://127.0.0.1:8081/   Quest: https://<jetson ip>:8443/quest"
