# Project rules

1. Python source contains no comments.
2. Do not use dashes or hyphens as punctuation in prose, documentation, UI copy or commit messages. Identifiers, paths, commands and URLs keep their spelling.
3. Never energize or move a physical arm without the user's explicit permission for that step. Describe what will move first. Test against the simulated bus in `tests/`.
4. Connecting must never enable motors. Stop holds position; never drop torque automatically.
5. No raw motor commands cross the network. Devices send poses and inputs; the arm computer plans and checks every motion.
6. Keep secrets in the local data folder (`~/.rebot-teleop/.env`), never in the repository.
7. The iPhone app brief is in `ios/AGENTS.md`.
8. Agents driving the robot read `docs/ROBOT_GUIDE.md` first. Agents move only the simulator unless the person at the robot sets `REBOT_ALLOW_REAL=1`, and agents never power arms on.

# Driving the robot

The `rebot` MCP server (`.mcp.json`, `scripts/rebot_mcp.sh`) gives tools to read state, look through cameras, move, run Python policies and run the task benchmark. It talks to the teleop server at `REBOT_URL` (default `http://127.0.0.1:8080`). Start a simulator with `.venv/bin/python -m rebot_teleop --sim headless` (add `--lan` to watch from a phone, or use `--sim gui` on a desktop to open the dashboard with its Simulation view). Policies live in `policies/<task>.py`; `python -m rebot_teleop.dojo` lists, resets and evaluates tasks.

# Continuing on the Jetson

`docs/JETSON_NEXT.md` is the handoff: what is built, what is verified, and the steps left to do on the real arms.
