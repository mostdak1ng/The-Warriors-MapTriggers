#!/usr/bin/env python3
"""Reversible queued 2D rendering toggle, including worlds without a player."""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import struct
import time

from pine_client import GAME_ID, UUID, PineClient
from overlay_status import StatusPublisher, read_status


# Dev graphics queued-panel pass. Keep camera begin/end, queue reset and
# DMA cleanup intact. The original beql executes its reset delay slot only
# for an empty queue; the replacement always takes that same empty path.
PATCH_ADDRESS = 0x00185E28
ORIGINAL = 0x50400055
HIDDEN = 0x10000055
CONTEXT_ADDRESS = 0x00185E20
CONTEXT = (0x24060004, 0x8EA21898, ORIGINAL, 0xAEA01898)

# Screen-color fade draw dispatch. Its caller still advances the fade and
# performs completion callbacks; only the final graphics call is omitted.
FADE_PATCH_ADDRESS = 0x0018CC48
FADE_ORIGINAL = 0x0060F809
FADE_HIDDEN = 0x00000000
FADE_CONTEXT_ADDRESS = 0x0018CC40
FADE_CONTEXT = (0x84440130, 0x8C430134, FADE_ORIGINAL, 0x00E42021)
PATCH_SPECS = (
    (PATCH_ADDRESS, CONTEXT_ADDRESS, CONTEXT, HIDDEN),
    (FADE_PATCH_ADDRESS, FADE_CONTEXT_ADDRESS, FADE_CONTEXT, FADE_HIDDEN),
)


class GameUiPanels:
    def __init__(self, client: PineClient):
        self.client = client
        self.hidden = False
        self.owned: set[int] = set()

    def validate_game(self) -> None:
        if (
            self.client.text(GAME_ID) != "SLUS-21215"
            or self.client.text(UUID).lower() != "b99a75de"
        ):
            raise RuntimeError("2D panels require The Warriors USA B99A75DE")

    def validate_patch(self, spec) -> int:
        address, context_address, context, _hidden = spec
        words = struct.unpack(
            "<4I", self.client.read_block(context_address, 16, progress=False)
        )
        if words[:2] != context[:2] or words[3] != context[3]:
            raise RuntimeError(f"2D panel signature does not match at 0x{address:08X}")
        return words[2]

    def set_hidden(self, hidden: bool) -> None:
        if hidden:
            self.validate_game()
            # Validate both sites before touching either one.
            current_words = [self.validate_patch(spec) for spec in PATCH_SPECS]
            for spec, current in zip(PATCH_SPECS, current_words):
                address, _base, context, replacement = spec
                if current != context[2] and not (address in self.owned and current == replacement):
                    raise RuntimeError(f"2D panel patch at 0x{address:08X} is owned by another patch")
            try:
                for spec, current in zip(PATCH_SPECS, current_words):
                    address, _base, context, replacement = spec
                    self.owned.add(address)
                    if current == context[2]:
                        self.client.write(address, replacement, 32)
                    if self.client.read(address, 32) != replacement:
                        raise RuntimeError(f"2D panel patch did not verify at 0x{address:08X}")
                self.hidden = True
            except Exception:
                self.close()
                raise
        elif self.owned:
            self.validate_game()
            errors = []
            for spec in reversed(PATCH_SPECS):
                address, _base, context, replacement = spec
                if address not in self.owned:
                    continue
                try:
                    current = self.validate_patch(spec)
                    if current == replacement:
                        self.client.write(address, context[2], 32)
                    elif current != context[2]:
                        raise RuntimeError(f"2D panel code changed at 0x{address:08X}; refusing to overwrite it")
                    if self.client.read(address, 32) != context[2]:
                        raise RuntimeError(f"2D panel restoration did not verify at 0x{address:08X}")
                    self.owned.remove(address)
                except Exception as error:
                    errors.append(str(error))
            self.hidden = bool(self.owned)
            if errors:
                raise RuntimeError("; ".join(errors))

    def close(self) -> None:
        self.set_hidden(False)


def write_status(path: Path | None, hidden: bool, error: str = "") -> None:
    if path is not None:
        StatusPublisher(path).publish({"hidden": hidden, "error": error})


class PanelHotkeys:
    """Use the host's PINE connection; PCSX2 serves one client at a time."""

    def __init__(self, client: PineClient, state_file: Path):
        self.controller = GameUiPanels(client)
        self.state_file = state_file
        self.publisher = StatusPublisher(state_file)
        self.key_was_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x4F) & 0x8000)
        self.next_check = 0.0
        self.preference = False
        self.preference = bool(read_status(state_file, pending=True).get("hidden", False))
        try:
            self.controller.set_hidden(self.preference)
            self.publisher.publish({"hidden": self.preference, "error": ""})
        except Exception:
            self.controller.close()
            raise

    def poll(self) -> bool:
        from freecam_probe import find_game_window, game_window_has_focus

        focused = game_window_has_focus(find_game_window())
        down = bool(ctypes.windll.user32.GetAsyncKeyState(0x4F) & 0x8000)
        if focused and down and not self.key_was_down:
            self.controller.set_hidden(not self.controller.hidden)
            self.preference = self.controller.hidden
            self.publisher.publish({"hidden": self.preference, "error": ""})
            print(f"O: HIDE GAME 2D {'ON' if self.preference else 'OFF'}", flush=True)
        self.key_was_down = down
        if time.monotonic() >= self.next_check:
            if self.controller.hidden:
                self.controller.set_hidden(True)
            self.publisher.publish({"hidden": self.preference, "error": ""})
            self.next_check = time.monotonic() + 0.25
        return bool(focused and ctypes.windll.user32.GetAsyncKeyState(0x79) & 0x8000)

    def close(self) -> None:
        try:
            self.controller.close()
        except Exception as error:
            self.publisher.publish({"hidden": self.preference, "error": str(error)})
            raise
        # Preserve the preference across renderer rebuilds, with original
        # executable bytes restored before handing PINE to the next process.
        self.publisher.publish({"hidden": self.preference, "error": ""})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=0)
    args = parser.parse_args()
    client = PineClient(port=args.port)
    hotkeys = None
    deadline = time.monotonic() + args.duration if args.duration > 0 else float("inf")
    try:
        hotkeys = PanelHotkeys(client, args.state_file)
        while time.monotonic() < deadline and not hotkeys.poll():
            time.sleep(0.01)
    finally:
        try:
            if hotkeys is not None:
                hotkeys.close()
        finally:
            client.close()


if __name__ == "__main__":
    main()
