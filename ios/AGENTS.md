# Agent brief: build and install the reBot Teleop iPhone app on a Mac

You are running on the user's Mac. Your job is to get the native iPhone teleoperation app in `ios/RebotTeleop` building and installed on the user's iPhone 16 Pro Max, fixing Swift compile errors if any. The robot, its CAN bus and the dashboard server run on a separate Linux machine; you do not run or change them from this Mac.

## Rules

1. Never send commands that move the robot. Do not call the dashboard's motion, hardware or WebSocket endpoints yourself. Reading `GET /health` to check reachability is fine.
2. Do not change the server code in `rebot_teleop/` or the WebSocket protocol below. If the app cannot work without a server change, stop and describe the change to the user instead.
3. Do not commit secrets, signing certificates, provisioning profiles, the generated `.xcodeproj`, `DerivedData` or build output.
4. Commits must be authored only by `ammarjmahmood` and contain no co-author or attribution lines. Do not use dashes or hyphens as punctuation in commit messages or prose. Keep commits small and push to `main` only when the user asks.
5. Ask the user before installing anything other than Xcode command line tools and XcodeGen through Homebrew.

## Steps

1. Pull the latest code: `git pull` in the repository root.
2. Check the toolchain: `xcode-select -p` and `xcodebuild -version` (Xcode 15 or newer). If Xcode is missing, ask the user to install it from the App Store and open it once.
3. Install XcodeGen if needed: `brew install xcodegen`.
4. Generate the project: `cd ios/RebotTeleop && xcodegen`.
5. Find the user's Apple development team ID. Ask the user if it is not obvious; Xcode shows it under Settings, Accounts. Do not write it into `project.yml` in a commit; pass it on the command line instead.
6. Build for a device:
   ```sh
   xcodebuild -project RebotTeleop.xcodeproj -scheme RebotTeleop -configuration Debug -destination 'generic/platform=iOS' -allowProvisioningUpdates DEVELOPMENT_TEAM=<TEAM_ID> -derivedDataPath build build
   ```
7. Fix any compile errors in `Sources/*.swift` and rebuild until it succeeds. Keep fixes minimal and keep the behavior described below.
8. Install on the phone. The phone must be connected by cable or paired wirelessly, unlocked, trusted, and have Developer Mode on (Settings, Privacy and Security, Developer Mode).
   ```sh
   xcrun devicectl list devices
   xcrun devicectl device install app --device <DEVICE_ID> build/Build/Products/Debug-iphoneos/RebotTeleop.app
   ```
   If command line signing or install fails, open `RebotTeleop.xcodeproj` in Xcode, select the team under Signing and Capabilities, choose the iPhone and press Run. The first launch may need the developer certificate trusted on the phone under Settings, General, VPN and Device Management.
9. Ask the user for the arm computer's address (shown on its control page under Show pairing code) and check it from the Mac with `curl http://<address>:8080/health`. A JSON reply means the phone can reach it too when on the same network.
10. Tell the user it is installed and how to use it (below). Report anything you could not verify.

## How the app is supposed to work

The design follows TidyBot++ phone teleoperation (https://github.com/jimmyyhwu/tidybot2, MIT licence, `policies.py`). No TidyBot++ code is copied.

1. Pairing: the user enters the computer address and the eight digit code shown on the dashboard under Teleoperation, Show pairing code. `POST /api/session {"token": code}` returns JSON with a `session` value, stored in the Keychain.
Scanning a pairing QR code on the control page opens `rebotteleop://pair?url=<address>&code=<code>`, which fills in both and pairs automatically.
2. The app opens `ws://<address>/ws/app` with the header `Authorization: Bearer <session>`.
3. The phone is held upright in portrait with the camera looking the way the user faces. The user picks where they stand relative to the arm and taps Start, which aligns forward to the camera's facing.
4. Thumb on the large pad enables motion and, on first touch, takes control and aligns. The gripper follows the phone's movement and rotation relative to the pose when the thumb went down. Lifting the thumb stops the arm. The Separate gripper strip toggle chooses between a strip on the left that sets the gripper without moving the arm (on) and a full width pad where sliding the thumb opens and closes the gripper while moving (off).
5. Losing tracking or the connection stops the arm on the server within a quarter of a second.

### WebSocket protocol (do not change)

Client to server, JSON text frames:

| type | fields |
|------|--------|
| `start` | `tilt` bool, `view` one of `behind`, `front`, `left`, `right` |
| `pose` | `p` [x, y, z] ARKit camera position in metres, `q` [w, x, y, z] ARKit camera rotation, `move` bool, `grip` 0 to 1 |
| `view` | `view` |
| `tilt` | `on` bool |
| `align` | none |
| `home` | none |
| `stop` | none |

Poses come from `ARFrame.camera.transform` with `ARWorldTrackingConfiguration.worldAlignment = .gravity`, sent at up to 60 Hz and only while tracking is normal.

Server to client every 100 ms: `{"type": "status", "owner", "active", "mode", "note", "calibrated", "tilt", "view", "torque", "fault", "remote", "joints" [degrees], "gripper", "phase"}`.

## Files

- `RebotTeleop/project.yml`: XcodeGen project, bundle id `com.ammarjmahmood.rebotteleop`, iOS 17, portrait, ARKit required, camera and local network usage strings, ATS allows local HTTP.
- `Sources/RebotTeleopApp.swift`: app entry, wires ARKit poses to the link.
- `Sources/ARTracker.swift`: ARKit session, tracking state, camera view.
- `Sources/ArmLink.swift`: pairing, Keychain, WebSocket, status decoding, haptics.
- `Sources/ContentView.swift`: pairing screen, controls, gripper strip, thumb pad, settings.
