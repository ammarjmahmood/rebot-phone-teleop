#!/usr/bin/env bash
set -e
source /opt/ros/humble/setup.bash
source "${ASCAM_WS:-$HOME/ascam_ros2_ws}/install/setup.bash"
cd "$(dirname "$0")/.."
ros2 launch ascamera hp60c.launch.py &
driver=$!
trap 'kill $driver 2>/dev/null' EXIT
python3 scripts/hp60c_bridge.py "$@"
