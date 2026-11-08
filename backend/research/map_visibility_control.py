"""Reversible exploration lighting/fog/range controls for The Warriors USA."""

from __future__ import annotations

import ctypes
import math
from pathlib import Path
import struct
import time

from pine_client import GAME_ID, UUID
from overlay_status import StatusPublisher, read_status


# RwRenderStateSet(rwRENDERSTATEFOGENABLE): take the existing disable branch.
# Both the GS primitive flag and RenderWare's cached flag are updated normally.
FOG_ADDRESS = 0x0048EA54
FOG_ORIGINAL = (0x12000006,)
FOG_CLEAR = (0x10000006,)
FOG_CONTEXT = (0xDF828730, 0x24040020, 0x93838780)

# Keep the complete native wrapper/stack frame. Only its JAL changes; immutable
# scaling entries live after the native camera stub in the renderer allocation.
FAR_ADDRESS = 0x00198910
FAR_ORIGINAL = (0x27BDFFF0, 0xFFBF0000, 0x0C120AD8, 0x8C840004,
                0xDFBF0000, 0x03E00008, 0x27BD0010, 0x00000000)
RANGE_FACTORS = (1, 2, 4, 8)
FAR_CONTEXT = (0x8C820004, 0x03E00008, 0xE44C0088, 0x00000000)
RANGE_STUB_OFFSET = 0xF6C
RANGE_STUB = (0x460C6300, 0x460C6300, 0x460C6300, 0x08120AD8, 0)
RANGE_STUB_END = RANGE_STUB_OFFSET + 4 * len(RANGE_STUB)  # F80, before identity matrix


def extend_range_blob(blob):
    result = bytearray(blob)
    if len(result) != 0x1000 or any(result[RANGE_STUB_OFFSET:RANGE_STUB_END]):
        raise ValueError('Range entries overlap the native camera stub')
    struct.pack_into('<5I', result, RANGE_STUB_OFFSET, *RANGE_STUB)
    return bytes(result)


def far_variant(base, factor):
    if factor == 1:
        return FAR_ORIGINAL
    if type(factor) is not int or factor not in RANGE_FACTORS:
        raise ValueError('Camera range must be 1, 2, 4 or 8')
    if base is None or base % 16 or not 0x700000 <= base < 0x1FFF000:
        raise RuntimeError('Camera range requires the owned native renderer')
    entry = base + RANGE_STUB_OFFSET + (3 - RANGE_FACTORS.index(factor)) * 4
    return (*FAR_ORIGINAL[:2], 0x0C000000 | (entry >> 2), *FAR_ORIGINAL[3:])

# Use the game's existing PVS-disabled render route and let its sector callback
# accept sectors independently of the selected cell. Both ordinary beq delay
# slots stay live. Frustum tests, queues and native draw callbacks are retained.
# No edits to sector masks, scene heap, or the game's global PVS preference.
PVS_SPECS = (
    (0x00411BC0, (0x10400008,), (0x10000008,),
     (0x24120001, 0x3C02005E, 0x8C43539C, 0x10600005)),
    (0x00411D54, (0x10400003,), (0x10000003,),
     (0x3C02005E, 0x10000002, 0x8C50539C, 0x0000802D)),
)

WORLD_GLOBAL = 0x0050CCE4
AMBIENT_OFFSET = 0x50
AMBIENT_MINIMUM = 1.0
AMBIENT_SIGNATURE_ADDRESS = 0x0017EBF4
AMBIENT_SIGNATURE = (0x7A420050, 0x0200282D, 0x0220202D, 0x0C05F210, 0x7FA20050)


def words(client, address, count):
    return tuple(client.read(address + index * 4, 32) for index in range(count))


def write_words(client, address, values):
    if len(values) == 1:
        client.write(address, values[0], 32)
    else:
        # One PINE request for the complete aligned code span.
        client.write_block(address, struct.pack(f"<{len(values)}I", *values))


class CodeSwitch:
    def __init__(self, client, address, original, replacement, context):
        self.client = client
        self.address = address
        self.original = original
        self.replacement = replacement
        self.context = context
        self.owned = False
        self.owned_replacements = set()

    def validate(self):
        current = words(self.client, self.address, len(self.original))
        context = words(self.client, self.address + len(self.original) * 4, len(self.context))
        if context != self.context:
            raise RuntimeError(f"Visibility signature mismatch at {self.address:08X}")
        owned_values = self.owned and all(
            value == self.original[index] or any(value == variant[index] for variant in self.owned_replacements)
            for index, value in enumerate(current)
        )
        if current != self.original and not owned_values:
            raise RuntimeError(f"Visibility code owned by another patch at {self.address:08X}")
        return current

    def set(self, enabled, replacement=None):
        if not enabled and not self.owned:
            return
        current = self.validate()
        target = (replacement or self.replacement) if enabled else self.original
        if current != target:
            self.owned = True  # retain cleanup ownership if transport fails
            self.owned_replacements.add(target)
            write_words(self.client, self.address, target)
            if words(self.client, self.address, len(target)) != target:
                raise RuntimeError(f"Visibility write did not verify at {self.address:08X}")
        self.owned = enabled
        if not enabled:
            self.owned_replacements.clear()


class RangeSwitch(CodeSwitch):
    def __init__(self, client, base):
        self.base = base
        super().__init__(client, FAR_ADDRESS+8, (FAR_ORIGINAL[2],), (),
                         FAR_ORIGINAL[3:]+FAR_CONTEXT)

    def validate(self):
        if words(self.client, FAR_ADDRESS, 2) != FAR_ORIGINAL[:2]:
            raise RuntimeError('Visibility range prologue is modified')
        if self.base is None or words(self.client, self.base+RANGE_STUB_OFFSET, 5) != RANGE_STUB:
            raise RuntimeError('Visibility range entries are missing or modified')
        return super().validate()


def disarm_range(client, base):
    """Also used by the supervisor if the freecam child could not restore it."""
    current = words(client, FAR_ADDRESS+8, 1)
    if current == (FAR_ORIGINAL[2],):
        return
    switch = RangeSwitch(client, base)
    switch.owned = True
    switch.owned_replacements = {(far_variant(base, factor)[2],) for factor in (2, 4, 8)}
    switch.set(False)


class VisibilityController:
    def __init__(self, client, renderer_base=None):
        self.client = client
        self.fog = CodeSwitch(client, FOG_ADDRESS, FOG_ORIGINAL, FOG_CLEAR, FOG_CONTEXT)
        self.far = RangeSwitch(client, renderer_base)
        self.pvs = tuple(CodeSwitch(client, *spec) for spec in PVS_SPECS)
        self.ambient_identity = None
        self.ambient_original = None
        self.ambient_target = None

    def validate_game(self):
        if self.client.text(GAME_ID) != "SLUS-21215" or self.client.text(UUID).lower() != "b99a75de":
            raise RuntimeError("Visibility controls require The Warriors USA B99A75DE")

    def world_identity(self):
        pointer = self.client.read(WORLD_GLOBAL, 32)
        if not 0x00700000 <= pointer <= 0x01FFFF00 or pointer % 16:
            return None
        # Scene and ambient light handles distinguish a recycled scene address.
        identity = (pointer, *words(self.client, pointer, 2), self.client.read(pointer + 0x40, 32))
        if not all(0x00700000 <= value < 0x02000000 for value in identity[1:]):
            return None
        return identity

    def ambient(self, enabled):
        identity = self.world_identity()
        if identity != self.ambient_identity:
            # The departed scene may already be freed. Never write its heap.
            self.ambient_identity = None
            self.ambient_original = None
            self.ambient_target = None
        if not enabled:
            if self.ambient_identity is not None:
                address = identity[0] + AMBIENT_OFFSET
                current = words(self.client, address, 3)
                for i, (value, target, original) in enumerate(zip(current, self.ambient_target, self.ambient_original)):
                    if value == target:
                        self.client.write(address + i * 4, original, 32)
                        if self.client.read(address + i * 4, 32) != original:
                            raise RuntimeError("Ambient restoration did not verify")
                self.ambient_identity = self.ambient_original = self.ambient_target = None
            return
        if words(self.client, AMBIENT_SIGNATURE_ADDRESS, len(AMBIENT_SIGNATURE)) != AMBIENT_SIGNATURE:
            raise RuntimeError("Native world ambient consumer signature mismatch")
        if identity is None:
            return
        address = identity[0] + AMBIENT_OFFSET
        current = words(self.client, address, 3)
        values = struct.unpack("<3f", struct.pack("<3I", *current))
        if not all(math.isfinite(v) and 0 <= v <= 16 for v in values):
            raise RuntimeError("Invalid world ambient color")
        if self.ambient_identity is None:
            self.ambient_identity = identity
            self.ambient_original = list(current)
            self.ambient_target = list(current)
        for i, (value, bits) in enumerate(zip(values, current)):
            if bits != self.ambient_target[i]:
                # A mission script or loaded state supplied a newer color.
                self.ambient_original[i] = bits
            target = struct.unpack("<I", struct.pack("<f", max(value, AMBIENT_MINIMUM)))[0]
            self.ambient_target[i] = target
            if bits != target:
                self.client.write(address + i * 4, target, 32)
                if self.client.read(address + i * 4, 32) != target:
                    raise RuntimeError("Ambient write did not verify")

    def apply(self, *, light=False, no_fog=False, extended=False, range_factor=None, no_pvs=False):
        factor = range_factor if range_factor is not None else (2 if extended else 1)
        if type(factor) is not int or factor not in RANGE_FACTORS:
            raise ValueError("Camera range must be 1, 2, 4 or 8")
        if light or no_fog or no_pvs or factor != 1 or self.fog.owned or self.far.owned or any(s.owned for s in self.pvs) or self.ambient_identity:
            self.validate_game()
        if no_pvs:
            # Reject either conflict before applying either PVS change.
            for switch in self.pvs:
                switch.validate()
        for switch in self.pvs:
            switch.set(no_pvs)
        self.fog.set(no_fog)
        self.far.set(factor != 1, (far_variant(self.far.base, factor)[2],))
        if light or self.ambient_identity:
            self.ambient(light)

    def close(self):
        if not (self.fog.owned or self.far.owned or any(s.owned for s in self.pvs) or self.ambient_identity):
            return
        self.validate_game()
        errors = []
        actions = [lambda: self.ambient(False), lambda: self.far.set(False), lambda: self.fog.set(False)]
        actions.extend(lambda s=s: s.set(False) for s in reversed(self.pvs))
        for action in actions:
            try:
                action()
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("; ".join(errors))


def status_path(panel_state: Path):
    return panel_state.with_suffix(".visibility.json")


class VisibilityHotkeys:
    KEYS = {"light": 0x5A, "no_fog": 0x58, "range_factor": 0x74, "no_pvs": 0x7A}  # Z/X/F5/F11

    def __init__(self, client, path, renderer_base=None):
        self.controller = VisibilityController(client, renderer_base)
        self.path = path
        self.publisher = StatusPublisher(path)
        saved = read_status(path, pending=True)
        factor = saved.get("range_factor", 2 if saved.get("extended") else 1)
        if type(factor) is not int or factor not in RANGE_FACTORS:
            factor = 1
        self.preference = {"light": bool(saved.get("light")), "no_fog": bool(saved.get("no_fog")), "range_factor": factor, "no_pvs": bool(saved.get("no_pvs"))}
        self.down = {k: self.key_down(v) for k, v in self.KEYS.items()}
        self.next_check = 0.0
        self.active = False
        self.publish()

    @staticmethod
    def key_down(vk):
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def publish(self, error=""):
        return self.publisher.publish({**self.preference, "active": self.active, "error": error})

    def poll(self, active=True):
        from freecam_probe import find_game_window, game_window_has_focus

        focused = game_window_has_focus(find_game_window())
        changed = active != self.active
        for key, vk in self.KEYS.items():
            down = self.key_down(vk)
            if focused and down and not self.down[key]:
                if key == "range_factor":
                    self.preference[key] = RANGE_FACTORS[(RANGE_FACTORS.index(self.preference[key]) + 1) % len(RANGE_FACTORS)]
                else:
                    self.preference[key] = not self.preference[key]
                changed = True
                label = f"{self.preference[key]}x" if key == "range_factor" else ("ON" if self.preference[key] else "OFF")
                print(f"VISIBILITY {key}: {label}", flush=True)
            self.down[key] = down
        if changed or time.monotonic() >= self.next_check:
            try:
                self.controller.apply(**(self.preference if active else {}))
                self.active = active
                self.publish()
            except Exception as error:
                self.publish(str(error))
                raise
            self.next_check = time.monotonic() + 0.25

    def close(self):
        try:
            self.controller.close()
        except Exception as error:
            self.publish(str(error))
            raise
        self.active = False
        self.publish()  # retain choices across the launcher's world rebuild
