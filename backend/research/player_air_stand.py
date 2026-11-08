#!/usr/bin/env python3
"""Keep the player on the grounded update route while explicitly enabled."""

from __future__ import annotations

import struct

from ingame_volume_probe import (
    beq,
    bne,
    jal,
    load_address,
    lw,
    run_heap_bootstrap,
)
from pine_client import PineClient
from player_noclip import resolve_player_object


HOOK_SPECS = (
    (0x00259648, 0x0C08997C),
    (0x002596A0, 0x0C08997C),
)
FALL_FLAG_TEST = 0x002265F0
STUB_OFFSETS = (0x000, 0x080)
DATA_OFFSET = 0x120
EMBEDDED_STUB_OFFSETS = (0x680, 0x6C0)
EMBEDDED_DATA_OFFSET = 0x700
EMBEDDED_END_OFFSET = 0x720
ENABLED_OFFSET = 0x00
HUMAN_OFFSET = 0x04
MARKER_OFFSET = 0x10
BLOB_SIZE = 0x180
MARKER = b"TW_AIR_STAND3"

ZERO = 0
V0 = 2
V1 = 3
A0 = 4
RA = 31


def jump(address: int) -> int:
    return 0x08000000 | ((address >> 2) & 0x03FFFFFF)


def jr(register: int) -> int:
    return register << 21 | 0x08


def daddu(destination: int, left: int, right: int) -> int:
    return (
        (left << 21)
        | (right << 16)
        | (destination << 11)
        | 0x2D
    )


def build_stub(
    base: int, hook_index: int, *, data_offset: int = DATA_OFFSET
) -> bytes:
    _hook, _original_word = HOOK_SPECS[hook_index]
    data = base + data_offset
    words: list[int] = []
    load_address(words, V0, data)
    words.append(lw(V1, ENABLED_OFFSET, V0))
    disabled_branch = len(words)
    words.extend((0, 0))
    words.append(lw(V0, HUMAN_OFFSET, V0))
    other_human_branch = len(words)
    words.extend((0, 0))
    words.extend((daddu(V0, ZERO, ZERO), jr(RA), 0))
    original_index = len(words)
    words[disabled_branch] = beq(
        V1, ZERO, original_index - disabled_branch - 1
    )
    words[other_human_branch] = bne(
        A0, V0, original_index - other_human_branch - 1
    )
    # Preserve the callsite's original return address: normal/other-human
    # traffic tail-jumps into the original leaf predicate.  This avoids a
    # nested call and never touches the EE's reserved K0/K1 registers.
    words.extend((jump(FALL_FLAG_TEST), 0))
    return struct.pack(f"<{len(words)}I", *words)


def build_blob(base: int) -> bytes:
    blob = bytearray(BLOB_SIZE)
    for index, stub_offset in enumerate(STUB_OFFSETS):
        code = build_stub(base, index)
        blob[stub_offset : stub_offset + len(code)] = code
    marker_start = DATA_OFFSET + MARKER_OFFSET
    blob[marker_start : marker_start + len(MARKER)] = MARKER
    return bytes(blob)


def write_embedded_layout(blob: bytearray, base: int) -> None:
    """Place the guard in the renderer's reserved executable gap."""
    if len(blob) < EMBEDDED_END_OFFSET:
        raise ValueError("renderer code block is too small for air stand")
    for index, stub_offset in enumerate(EMBEDDED_STUB_OFFSETS):
        code = build_stub(
            base, index, data_offset=EMBEDDED_DATA_OFFSET
        )
        end = stub_offset + len(code)
        if end > EMBEDDED_DATA_OFFSET:
            raise ValueError("air-stand stubs overlap their shared data")
        blob[stub_offset:end] = code
    marker_start = EMBEDDED_DATA_OFFSET + MARKER_OFFSET
    blob[marker_start : marker_start + len(MARKER)] = MARKER


class PlayerAirStandController:
    """Suppress only the player's two airborne-route checks when enabled."""

    def __init__(
        self,
        client: PineClient,
        *,
        enabled: bool = False,
        embedded_code_address: int = 0,
    ) -> None:
        self.client = client
        self.code_address = 0
        self.owns_code = not embedded_code_address
        self.stub_offsets = (
            STUB_OFFSETS if self.owns_code else EMBEDDED_STUB_OFFSETS
        )
        self.data_offset = (
            DATA_OFFSET if self.owns_code else EMBEDDED_DATA_OFFSET
        )
        self.installed_hooks: list[int] = []
        self.enabled = False
        self.human = 0
        try:
            for hook, original in HOOK_SPECS:
                current = client.read(hook, 32)
                if current != original:
                    raise RuntimeError(
                        f"fall-route callsite 0x{hook:08X} is not "
                        f"original: 0x{current:08X}"
                    )
            if self.owns_code:
                self.code_address = run_heap_bootstrap(client, BLOB_SIZE)
                client.write_block(
                    self.code_address, build_blob(self.code_address)
                )
            else:
                self.code_address = embedded_code_address
                marker = client.read_block(
                    self.code_address
                    + self.data_offset
                    + MARKER_OFFSET,
                    16,
                    progress=False,
                )
                if not marker.startswith(MARKER):
                    raise RuntimeError(
                        "renderer does not contain the air-stand layout"
                    )
            self.refresh_human()
            for (hook, _original_word), stub_offset in zip(
                HOOK_SPECS, self.stub_offsets
            ):
                patched = jal(self.code_address + stub_offset)
                client.write(hook, patched, 32)
                self.installed_hooks.append(hook)
                if client.read(hook, 32) != patched:
                    raise RuntimeError(
                        f"fall-route callsite 0x{hook:08X} failed "
                        "verification"
                    )
            self.set_enabled(enabled)
        except Exception:
            self.close()
            raise

    @property
    def data_address(self) -> int:
        if not self.code_address:
            raise RuntimeError("air-stand controller is closed")
        return self.code_address + self.data_offset

    def refresh_human(self) -> int:
        human = resolve_player_object(self.client)
        if human != self.human:
            self.human = human
            self.client.write(self.data_address + HUMAN_OFFSET, human, 32)
        return human

    def set_enabled(self, enabled: bool) -> bool:
        self.refresh_human()
        self.client.write(
            self.data_address + ENABLED_OFFSET, int(enabled), 32
        )
        self.enabled = enabled
        return enabled

    def toggle(self) -> bool:
        return self.set_enabled(not self.enabled)

    def close(self) -> None:
        if self.code_address:
            try:
                self.client.write(
                    self.code_address
                    + self.data_offset
                    + ENABLED_OFFSET,
                    0,
                    32,
                )
            except Exception:
                pass
        originals = dict(HOOK_SPECS)
        for hook in reversed(self.installed_hooks):
            current = self.client.read(hook, 32)
            expected_patch = jal(
                self.code_address + self.stub_offsets[
                    tuple(spec[0] for spec in HOOK_SPECS).index(hook)
                ]
            )
            if current == expected_patch:
                self.client.write(hook, originals[hook], 32)
            elif current != originals[hook]:
                raise RuntimeError(
                    f"air-stand hook ownership lost at 0x{hook:08X}: "
                    f"0x{current:08X}"
                )
        self.installed_hooks.clear()
        if self.code_address and self.owns_code:
            run_heap_bootstrap(
                self.client, self.code_address, freeing=True
            )
        self.code_address = 0
        self.enabled = False
        self.human = 0
