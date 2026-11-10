# Project rules

1. Python source contains no comments.
2. Do not use dashes or hyphens as punctuation in prose, documentation, UI copy or commit messages. Identifiers, paths, commands and URLs keep their spelling.
3. No ROS. Everything is Python on the Jetson; device SDKs are called directly (ctypes for the Angstrong camera).
4. When changing code, test with `python -m pytest -q tests` and `python -m rebot_teleop --sim` before touching the real arms. Ask the person at the robot before the first motion of new code on hardware.
5. Power on holds an arm where it is. Stop holds position; nothing drops torque on its own.
6. No raw motor commands cross the network. Clients send poses, joint goals or hand poses; `arm.py` turns them into motor commands.
7. Keep secrets in `~/.rebot-teleop/`, never in the repository.

# Driving the robot

The `rebot` MCP server (`scripts/rebot_mcp.sh`) is registered for every Claude Code session on the Jetson. It talks to the server at `REBOT_URL` (default `http://127.0.0.1:8081`). Read `docs/ROBOT_GUIDE.md` before driving. The operator can watch everything on the dashboard.
