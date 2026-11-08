# The Warriors MapTriggers

Free camera and map exploration tools for **The Warriors on PCSX2**.
MapTriggers succeeds The Warriors Freecam and includes its keyboard/mouse and
controller camera controls in one application.

- Native free camera with live camera and player coordinates.
- Mission trigger boxes, flag positions and an inside-volume panel.
- Live terrain collision, player bounds and climb diagnostics.
- Player carry/noclip, God mode and reversible practical world pause.
- Exploration lighting, fog removal, extended view range and sector visibility.
- Portable Windows x64 package with Python and .NET included.

Version **0.3.2**.
It connects through PCSX2's local PINE server and does not require a 60 fps patch.

## Requirements

- Windows 10 or Windows 11, x64.
- PCSX2 with PINE enabled (default slot `28011`). This version was
  tested with **PCSX2 v2.9.146**; compatibility with other builds
  has not been established for this MapTriggers release.
- Your own unmodified The Warriors USA v1.03 ISO9660 image: SLUS-21215,
  game CRC B99A75DE. Compressed images are not supported by the extractor.
- A complete extracted MapTriggers distribution. Python and .NET are included;
  neither needs to be installed separately on the destination PC.
- Borderless fullscreen or windowed mode for the external HUD. Exclusive
  fullscreen cannot display the external overlay.

The disc executable is verified before extraction. The application generates
its map catalogs privately from your image and caches them under
`%LOCALAPPDATA%\TheWarriorsMapTriggers`. No game image, BIOS, emulator,
savestate, Lua script, or extracted map dataset is included in the distribution.
No data or logs are uploaded automatically.

## Install and run

1. Download the Windows x64 ZIP and extract the **entire folder**. Keep
   `TheWarriorsMapTriggers.exe`, `backend` and `runtime` together.
2. In PCSX2, open **Settings > Advanced > PINE Settings**, enable PINE and note
   its slot/port. Restart PCSX2 after enabling it.
3. Use borderless fullscreen or windowed display. Optionally enable
   **Hide Cursor In Fullscreen** in PCSX2's interface settings for mouse look.
4. Load the game and create a backup savestate **before** starting MapTriggers.
5. Open `TheWarriorsMapTriggers.exe`, select your ISO and match the PINE port.
   Choose a start profile and any optional settings described below.
6. Acknowledge the backup warning and press **Start MapTriggers**. The first
   start generates the map cache from your ISO; later starts verify and reuse it.
7. Focus the PCSX2 game window to enable camera controls. If map detection is
   ambiguous, choose the loaded map in the launcher's selection dialog.

![PCSX2 PINE settings](docs/images/pcsx2-pine-settings.png)
![PCSX2 borderless fullscreen](docs/images/pcsx2-borderless-fullscreen.png)
![PCSX2 cursor setting](docs/images/pcsx2-hide-cursor.png)

There is no mod file to install in PCSX2's cheats folder, and administrator
rights are not required. Existing Freecam users should extract MapTriggers
into a separate folder; no previous Freecam installation is needed.

## Stop safely and protect your savestates

Exit with **F10**, **Stop safely**, or the controller exit chord. Closing the
launcher requests the same restoration; wait for it to finish.
If the game pause menu is open or PCSX2 is paused, close the menu or resume
PCSX2 when requested. A second Stop/F10 ends the wait with hooks detached and
retained memory; restart the game before another session in that case.

**Do not create or load savestates while the mod is running.** Temporary hooks,
player state and timestep changes can be captured in them. Close MapTriggers
before loading another state. Back up your memory card before experimenting
with player relocation. If a savestate is loaded accidentally while the mod is
active, exit MapTriggers, restart the game and load the backup made before the
session rather than creating another state.

Do not run MapTriggers alongside the old Freecam or another MapTriggers
session: they share native hooks. If ownership is lost or restoration fails,
review the log and restart the game before retrying.

## Profiles and launcher options

MapTriggers starts in keyboard/mouse Freecam, carry off and God mode on. The
**Freecam** profile starts with diagnostic geometry disabled and a compact HUD;
**MapTriggers** starts with trigger diagnostics. Both use the same native camera.
Experimental X-ray/object collision is optional and starts disabled.

| Option | Behavior |
| --- | --- |
| Capture Pad 1 in keyboard/mouse mode | Prevents gameplay input from keys that PCSX2 also maps to Pad 1. Leave off to let a controller play alongside keyboard/mouse Freecam. |
| Return to player FollowCamera when leaving Freecam | Explicitly selects the player's follow camera when leaving Freecam or exiting. Off by default because it can replace fixed or scripted cameras. |
| Start with the world practically paused | Applies the practical pause as soon as the renderer is installed. |
| Experimental collision | Enables X-ray drawing and the optional nearby-object bounds view. |

The selected ISO, port, profile and options are saved between runs. You can
also start in a menu or Game Over: **O** and **F10** remain available while
MapTriggers waits for a playable world. Camera and geometry tools start when
the required world and allocator pass are ready.

## Keyboard and mouse

Controls are active while the PCSX2 game window has focus. **R** affects the
informational HUD, including labels and coordinates; native diagnostic geometry
uses its own filters below. No version watermark is drawn over gameplay.

| Input | Action |
| --- | --- |
| Mouse; W/A/S/D; Q/E | Look; move; down/up |
| Shift / Ctrl | Fast / precise movement |
| V | Keyboard/mouse Freecam / normal game camera |
| P | Practical world pause / restore time |
| F8 | Player carry/noclip |
| G | God mode |
| R | Show/hide the informational HUD |
| F10 | Safe exit |
| F | Trigger flags / all flags / off |
| 0-3 | Trigger kinds |
| N / Shift+N; M | Next/previous trigger; selected trigger only |
| 4-9; apostrophe | Terrain categories; all terrain |
| C; T; Y | Climb diagnostics; player bounds; player air stand |
| O | Hide/show native game 2D panels |
| Z; X | Exploration light; remove fog |
| F5 | View range 1x / 2x / 4x / 8x |
| F11 | All loaded sectors / native visibility |
| B | Nearby object bounds (experimental collision option) |

Hiding game panels changes drawing, not menu logic: hidden menus may still
receive input. All-sectors mode does not load absent content or remove every
LOD/occlusion rule. Extra range and geometry can reduce performance. The
terrain batch retains its existing 44-face limit and object bounds its 24-box
limit; these are diagnostics, not a complete collision export.

## Controller (PCSX2 Pad 1)

The mod uses the controller already configured in PCSX2, including SDL mappings;
it does not require an additional controller library. Radial deadzone is 10%.
Button names below follow the PS2 layout: on a standard Xbox mapping,
**Select = View/Back**, **Start = Menu**, **Square = X**, **Circle = B** and
**Triangle = Y**. **L1/R1 = LB/RB**, **L2/R2 = LT/RT** and **L3/R3** are the
stick clicks. These refer to your configured PCSX2 mappings. This version
has been tested with an Xbox controller mapped in PCSX2, keyboard and mouse.

| Input | Action |
| --- | --- |
| Select+L3 | Controller Freecam / normal game camera |
| Left / right stick | Move / look |
| L1 / R1 | Down / up |
| L2 / R2 | Fast / precise movement |
| Select+Square | World pause |
| Select+R3 | Carry |
| Select+Circle | God mode |
| Select+Triangle | Informational HUD |
| Hold Select+Start 1.5 seconds, then release both | Safe exit |

Pad 1 is neutralized for gameplay while controlling Freecam with the controller.
Normal mode, focus loss and world transitions release capture. If the host stops
refreshing suppression, the guest routine returns input after 60 refresh calls.
Use V to switch from controller Freecam to keyboard/mouse without leaving
Freecam. The optional keyboard capture setting also neutralizes Pad 1 while
keyboard/mouse Freecam is active; otherwise a controller can play alongside it.
Advanced diagnostics retain keyboard controls.

By default, leaving Freecam lets the game's currently selected camera resume.
The optional FollowCamera setting instead explicitly selects the player's
follow camera, including at exit; it can replace fixed or scripted cameras.

## Map data and diagnostic limits

The extractor produces a catalog of **53 datasets**, containing **974 trigger
boxes** and **6,196 flag positions**, across story missions, flashbacks, Armies
of the Night, Coney and auxiliary scripts. This describes static extraction
coverage, not gameplay testing of every level. A flag position does not imply
that its mission handler is currently active; associated trigger radii are
shown when the extractor can recover them.

Map selection is normally automatic. The manual dialog includes auxiliary
datasets when detection cannot confidently choose one. Terrain and object
bounds come from the running game. Split-screen/P2, the full campaign and
loading arbitrary savestates with active hooks have not been validated.

## Pause, transitions and restoration

World pause scales the captured world timestep to 0.01%; it is a practical
pause, not a complete simulation freeze. It restores the exact original float
bits when toggled off, during world transitions and on clean exit. The camera
continues updating. **Start with the world practically paused** enables this
as soon as the renderer is installed; if PCSX2 was paused, resume it when
requested so allocation can complete.

Transitions detach native hooks and release controller capture before rebuilding
for a stable world. F10/Stop during a transition can wait for a safe allocator
pass before releasing owned heap blocks. Do not force-close the backend.
Carry does not move the player back to the starting position at exit. God and
no-target bits are restored for still-valid player entities. Camera and renderer
allocations are released through the game's allocator. This is not a guarantee
of arbitrary active-savestate migration or crash-proof recovery.

## Logs and troubleshooting

Use **Open logs** in the launcher. Logs, settings, cached data and session files
live under `%LOCALAPPDATA%\TheWarriorsMapTriggers`; the extracted installation
can be read-only. Logs contain technical paths, emulator identity, coordinates,
input state, timings and errors. Review them before sharing. When reporting a
problem, include the MapTriggers version from the launcher, PCSX2 version,
game/mission, relevant settings and the newest log.

- PINE unavailable: verify the selected port, enable PINE and restart PCSX2.
- Unsupported image/game: use the supported USA executable and your own ISO.
- Occupied hook: close other tools and restart the game; do not force takeover.
- Invisible HUD: focus the game and use borderless/windowed display.
- Incomplete distribution: extract the full ZIP, not just its executable.
- Unsigned build: this build is not code-signed.
- No camera movement: focus the game window; use **V** for keyboard/mouse or
  **Select+L3** for controller Freecam, and check that Pad 1 works in PCSX2.
- ISO preparation failed: select the supported unmodified `.iso` image.
  Compressed images such as CHD/CSO cannot be used by this extractor.

## Build from source

Install .NET 8 SDK, CPython **3.14.4 x64 with Tcl/Tk**, and pytest for development.
Then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-release.ps1
```

Use `-Offline` if the pinned .NET 8.0.15 runtime packages are already available
in the local package cache or `.offline-packages`. The build runs the core and
Python tests, publishes the self-contained launcher, includes an isolated Python
runtime, tests imports without PATH, and writes binary/source ZIPs plus SHA-256
checksums under `artifacts/maptriggers-local`. It does not commit, tag, push,
rename the GitHub repository or publish a release. The runtime versions and
whether tests ran are recorded in `build.json`.

Disc-data tests are optional in a source-only build: set
`TW_MAPTRIGGERS_TEST_CATALOG` to a locally generated `lua_volume_catalog.json`
to exercise them. Extracted datasets are never copied into source archives.

## License

Copyright (C) 2026 mostdak1ng. Source: [**GPL-3.0-only**](LICENSE).
Provide the matching Corresponding Source when redistributing binaries as
required by GPLv3. Included .NET, Python, Tcl/Tk and native runtime components
retain their own terms; see [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)
and `runtime/LICENSE.txt` in the Windows package. See [SOURCE.txt](SOURCE.txt)
for the matching source archive.

Use the mod at your own risk. The author is not responsible for damage to a
game save, memory card, or save state.
