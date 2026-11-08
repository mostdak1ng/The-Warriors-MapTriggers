# Changelog

## 0.3.2 — 2026-11-07

- Renamed the application and executable to The Warriors MapTriggers and
  `TheWarriorsMapTriggers.exe`.
- Added mission trigger boxes, flag positions, live terrain collision and map
  diagnostics, plus native camera controls and exploration visibility tools.
- Removed the permanent gameplay watermark and lower-left status footer.
- Moved logs and settings from the executable's `logs\` directory to
  `%LOCALAPPDATA%\TheWarriorsMapTriggers`.
- Added first-run map extraction from a supported, separately supplied ISO.
- Bundled Python, Tcl/Tk and .NET: approximately 83 MB compressed and 200 MB
  extracted, with no separate runtime installation required.
- Added persistent settings, start profiles and a map-selection dialog.
- Preserve native stack frames during range changes and exit restoration.
- Wait for gameplay to resume before releasing memory when exiting from the
  game pause menu or paused PCSX2. A second Stop/F10 can end that wait.
- Retire allocator shims before clearing their executable scratch and prevent
  retrying a heap release whose completion is uncertain.
- Match manual map responses to unique requests and ignore duplicate dialogs.

## 0.2.0 — 2026-08-18

- Added reversible world pause, off by default, scaling the native world
  timestep to 0.01%.
- Added `P` as the keyboard toggle in every active camera mode and
  `Select+Square` as the controller-Freecam toggle.
- Added the world-pause state and mode-appropriate hotkeys to the overlay.
- World pause restores the exact original timestep when toggled off, before a
  world transition, and on clean exit; it remains off after the new map loads.
- Standardized controller labels on the PS2 layout.

## 0.1.6 — 2026-08-18

- First public release.
- Added keyboard/mouse and Pad 1 Freecam control modes with a 10% controller
  deadzone.
- Added live camera/player coordinates, player carry, reversible God mode,
  and a permanent version watermark.
- Added reversible, fail-open native Pad 1 capture, including an optional
  launcher setting for PCSX2 keyboard mappings.
- Returning to normal camera mode now stops overriding the game's active
  camera by default instead of forcing the generic player FollowCamera. This
  preserves scripted and cutscene camera behavior.
- Added an opt-in launcher setting to return to the player FollowCamera for
  users who prefer it after relocating the player.
- Added preflight validation, clean shutdown, state restoration, and detailed
  local diagnostic logs.
- Confirmed support for PCSX2 stable and PCSX2 2.7.522 nightly.
- Clarified supported PCSX2 channels, loaded-map requirements, memory-card
  backup guidance, recommended emulator settings, and troubleshooting.
- Documented that PCSX2 must be restarted after enabling its PINE server.
- Documented exclusive-fullscreen overlay limitations as a known issue.
