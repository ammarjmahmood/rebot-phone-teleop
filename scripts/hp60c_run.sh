#!/usr/bin/env bash
source /opt/ros/humble/setup.bash
source "${ASCAM_WS:-$HOME/ascam_ros2_ws}/install/setup.bash"
cd "$(dirname "$0")/.."
out="${REBOT_TORSO_DIR:-/dev/shm/rebot-torso}"
stale="${REBOT_TORSO_STALE_S:-10}"
driver=""
bridge=""
stop_all() {
  [ -n "$bridge" ] && kill "$bridge" 2>/dev/null
  [ -n "$driver" ] && kill "$driver" 2>/dev/null
  wait 2>/dev/null
}
trap 'stop_all; exit 0' EXIT INT TERM
while true; do
  ros2 launch ascamera hp60c.launch.py &
  driver=$!
  python3 scripts/hp60c_bridge.py "$@" &
  bridge=$!
  sleep 20
  while kill -0 "$driver" 2>/dev/null && kill -0 "$bridge" 2>/dev/null; do
    last=$(stat -c %Y "$out/color.jpg" 2>/dev/null || echo 0)
    if [ $(( $(date +%s) - last )) -gt "$stale" ]; then
      echo "Torso camera frames are older than $stale s; restarting the driver and the bridge"
      break
    fi
    sleep 2
  done
  stop_all
  sleep 3
done
