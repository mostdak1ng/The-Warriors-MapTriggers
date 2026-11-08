"""Pad 1 capture shared with MapTriggers' native renderer lifecycle.

The Freecam v0.2.0 routine is relocated into two unused renderer slots with
one extra jump. Its static scratch is reserved exclusively for the allocator.
No XInput dependency: the user's existing PCSX2 Pad 1 mappings supply input.
"""
from __future__ import annotations

import json
import math
import struct
import time
from pathlib import Path

HOOK = 0x1498E0
ORIGINAL = 0x0C0525DC
ENTRY_OFFSET = 0x640
TAIL_OFFSET = 0x7C0
CAPTURE_OFFSET = 0x7F0
TTL_OFFSET = 0x7F8
MARKER_OFFSET = 0x7FC
MARKER_VALUE = 0x434D5754  # TWMC, distinct from retired standalone Freecam.
SELECT, L3, R3, START = 0x100, 0x200, 0x400, 0x800
L2, R2, L1, R1 = 1, 2, 4, 8
TRIANGLE, CIRCLE, SQUARE = 0x10, 0x20, 0x80


def build_blobs(base):
    capture = base + CAPTURE_OFFSET
    upper = ((capture + 0x8000) >> 16) & 0xFFFF
    low = capture & 0xFFFF
    first = [
        0x27BDFFE0, 0xFFBF0010, 0x3C08005E, 0x3C090000 | upper,
        0xDD2A0000 | low, 0xFD0AE4D0, 0x0C0525DC, 0,
        0x3C08005E, 0x3C090000 | upper, 0xDD0AE4D0, 0xFD2A0000 | low,
        0x8D2A0000 | ((capture + 8) & 0xFFFF),
        0x08000000 | ((base + TAIL_OFFSET) >> 2), 0, 0,
    ]
    tail = [
        0x11400007, 0x254AFFFF, 0xAD2A0000 | ((capture + 8) & 0xFFFF),
        0x3C0A7F7F, 0x354A7F7F, 0xFD0AE4D0, 0xFD00E4D8,
        0xA500E4E0, 0xDFBF0010, 0x03E00008, 0x27BD0020, 0,
        0x7F7F7F7F, 0, 0, MARKER_VALUE,
    ]
    return struct.pack('<16I', *first), struct.pack('<16I', *tail)


class PadCapture:
    def __init__(self, client, base):
        if base % 16 or not 0x700000 <= base < 0x1FFF000:
            raise ValueError('Pad capture requires an aligned renderer allocation')
        self.client, self.base = client, base
        self.entry, self.tail = base + ENTRY_OFFSET, base + TAIL_OFFSET
        self.capture, self.ttl, self.marker = base + CAPTURE_OFFSET, base + TTL_OFFSET, base + MARKER_OFFSET
        self.jump = 0x0C000000 | (self.entry >> 2)
        self.first_blob, self.tail_blob = build_blobs(base)
        self.installed = False
        self.next_check = self.next_refresh = 0.0
        self.suppressed = False

    def code_matches(self):
        p = self.client
        return (p.read_block(self.entry, 0x40, progress=False) == self.first_blob
                and p.read_block(self.tail, 0x30, progress=False) == self.tail_blob[:0x30]
                and p.read(self.marker, 32) == MARKER_VALUE)

    def owned(self):
        return self.client.read(HOOK, 32) == self.jump and self.code_matches()

    def install(self):
        p = self.client
        occupied = any(p.read_block(self.entry, 0x40, progress=False)) or any(p.read_block(self.tail, 0x40, progress=False))
        inert = occupied and self.code_matches()
        if p.read(HOOK, 32) != ORIGINAL or (occupied and not inert):
            raise RuntimeError('Pad capture reservation is occupied. Close other tools and restart the game.')
        if not inert:
            p.write_block(self.entry, self.first_blob)
            p.write_block(self.tail, self.tail_blob)
        p.write(self.ttl, 0, 32)
        if not self.code_matches():
            raise RuntimeError('Pad code verification failed before activation')
        self.installed = True
        p.write(HOOK, self.jump, 32)
        if not self.owned():
            raise RuntimeError('Pad hook verification failed')

    def read(self, suppress):
        now = time.monotonic()
        if not self.installed or (now >= self.next_check and not self.owned()):
            raise RuntimeError('Pad ownership lost; a savestate may have been loaded. Restart the game.')
        if now >= self.next_check:
            self.next_check = now + 0.25
        data = self.client.read_block(self.capture, 8, progress=False)
        if suppress != self.suppressed or now >= self.next_refresh:
            self.client.write(self.ttl, 60 if suppress else 0, 32)
            self.suppressed = suppress
            self.next_refresh = now + 0.10
        return struct.unpack('<4BH', data[:6])

    def release(self):
        if self.installed:
            if not self.owned():
                raise RuntimeError('Refusing to restore another Pad owner')
            self.client.write(self.ttl, 0, 32)
            self.suppressed = False

    def close(self):
        if not self.installed:
            return
        if self.client.read(HOOK, 32) == ORIGINAL:
            self.installed = False
            return
        self.release()
        self.client.write(HOOK, ORIGINAL, 32)
        if self.client.read(HOOK, 32) != ORIGINAL:
            raise RuntimeError('Pad native call restoration failed')
        # Keep executable words until the supervisor frees the renderer through
        # the next safe allocator pass. Never reuse the allocator's static gap.
        self.installed = False


def disarm(client, base):
    if client.read(HOOK, 32) == ORIGINAL:
        return
    pad = PadCapture(client, base)
    pad.installed = True
    pad.close()


def stick(x, y, invert_y=False):
    x, y = (x - 127.5) / 127.5, (y - 127.5) / 127.5
    length = math.hypot(x, y)
    if length <= 0.10:
        return 0.0, 0.0
    scale = min(1.0, (length - 0.10) / 0.90) / length
    return x * scale, y * scale * (-1 if invert_y else 1)


class ControllerInput:
    def __init__(self, client, base, capture_keyboard=False, state_path: Path | None = None):
        self.pad = PadCapture(client, base)
        self.capture_keyboard = capture_keyboard
        self.state_path = state_path
        from overlay_status import StatusPublisher
        self.publisher = StatusPublisher(state_path) if state_path is not None else None
        self.controller_mode = False
        self.previous_buttons = 0
        self.exit_since = None
        self.exit_armed = False
        self.exit_requested = False
        self.pulses = set()
        self.move = (0.0, 0.0, 0.0)
        self.look = (0.0, 0.0)
        self.speed_factor = 1.0
        self.requested_enabled = None
        self.hud_toggle = 0
        self.last_status = None
        self.focused = False

    def start(self):
        self.pad.install()

    def sample(self, now, enabled, focused, transitioning=False, keyboard_v=False):
        self.focused = focused
        self.pulses.clear()
        self.requested_enabled = None
        self.move = (0.0, 0.0, 0.0)
        self.look = (0.0, 0.0)
        self.speed_factor = 1.0
        if not self.pad.installed:
            self.exit_since = None
            self.exit_armed = False
            return
        suppress = focused and not transitioning and enabled and (
            self.controller_mode or self.capture_keyboard)
        lx, ly, rx, ry, buttons = self.pad.read(suppress)
        self.process(lx, ly, rx, ry, buttons, now, enabled, focused, transitioning, keyboard_v)
        next_enabled = enabled if self.requested_enabled is None else self.requested_enabled
        should_suppress = focused and not transitioning and next_enabled and (
            self.controller_mode or self.capture_keyboard)
        if should_suppress != suppress:
            self.client_suppression(should_suppress)

    def client_suppression(self, suppress):
        self.pad.client.write(self.pad.ttl, 60 if suppress else 0, 32)
        self.pad.suppressed = suppress

    def process(self, lx, ly, rx, ry, buttons, now, enabled, focused,
                transitioning=False, keyboard_v=False):
        def chord(action):
            mask = SELECT | action
            return buttons & mask == mask and self.previous_buttons & mask != mask

        if not focused or transitioning:
            self.exit_since = None
            self.exit_armed = False
            self.previous_buttons = buttons
            return
        if keyboard_v:
            self.requested_enabled = True if self.controller_mode else not enabled
            self.controller_mode = False
        if not self.exit_armed and chord(L3):
            self.requested_enabled = not self.controller_mode
            self.controller_mode = not self.controller_mode
        if self.controller_mode:
            if buttons & (SELECT | START) == SELECT | START:
                if self.exit_since is None:
                    self.exit_since = now
                if now - self.exit_since >= 1.5:
                    self.exit_armed = True
            elif self.exit_armed:
                if not buttons & (SELECT | START):
                    self.exit_requested = True
            else:
                self.exit_since = None
            if not self.exit_armed:
                for action, key in ((R3, 'F8'), (CIRCLE, 'G'), (SQUARE, 'P')):
                    if chord(action):
                        self.pulses.add(key)
                if chord(TRIANGLE):
                    self.hud_toggle += 1
                if not buttons & SELECT:
                    x, y = stick(lx, ly, True)
                    self.move = (x, y, float(bool(buttons & R1)) - float(bool(buttons & L1)))
                    self.look = stick(rx, ry)
                    self.speed_factor = (4.0 if buttons & L2 else 1.0) * (0.25 if buttons & R2 else 1.0)
        self.previous_buttons = buttons

    def publish(self, enabled, world_paused=False):
        if self.state_path is None:
            return
        state = dict(controller=self.controller_mode, freecam=enabled,
                     exit_armed=self.exit_armed, hud_toggle=self.hud_toggle,
                     focused=self.focused, move=self.move, look=self.look,
                     buttons=self.previous_buttons, world_paused=world_paused)
        self.publisher.publish(state)
