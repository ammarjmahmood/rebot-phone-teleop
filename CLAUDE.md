# Project rules

1. Python source contains no comments.
2. Do not use dashes or hyphens as punctuation in prose, documentation, UI copy or commit messages. Identifiers, paths, commands and URLs keep their spelling.
3. Never energize or move a physical arm without the user's explicit permission for that step. Describe what will move first. Test against the simulated bus in `tests/`.
4. Connecting must never enable motors. Stop holds position; never drop torque automatically.
5. No raw motor commands cross the network. Devices send poses and inputs; the arm computer plans and checks every motion.
6. Keep secrets in the local data folder (`~/.rebot-teleop/.env`), never in the repository.
7. The iPhone app brief is in `ios/AGENTS.md`.
