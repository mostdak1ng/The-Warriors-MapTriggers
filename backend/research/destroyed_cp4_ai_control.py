#!/usr/bin/env python3
"""Reversible brain suspension for Destroyed checkpoint 4 actors.

The native ``BrDead`` implementation sets ``Brain + 0x09``.  Mission tactics
can later assign another target and clear that state, so this controller
reasserts it while a freeze mode is enabled.  It never changes HP or calls a
Human death routine.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

from destroyed_cp4_monitor import Actor
from pine_client import WRITE8, PineClient


BRAIN_BASE = 0x006D53F0
BRAIN_STRIDE = 0x2F0
BRAIN_DEAD_OFFSET = 0x09
HUMAN_MOVE_INDEX_OFFSET = 0x92
HUMAN_VTABLE = 0x0053F088

MODE_OFF = "off"
MODE_ALL = "all"
MODE_EXCEPT_OOB = "except_oob"
MODES = {MODE_OFF, MODE_ALL, MODE_EXCEPT_OOB}
OOB_LIEUTENANTS = {"Destroyer2", "Destroyer3"}


def aligned_range(address: int, size: int) -> tuple[int, int]:
    base = address & ~7
    end = (address + size + 7) & ~7
    return base, end - base


def select_targets(
    actors: Iterable[Actor], *, mode: str
) -> tuple[Actor, ...]:
    if mode not in MODES:
        raise ValueError(f"invalid AI mode: {mode}")
    if mode == MODE_OFF:
        return ()
    result = []
    for actor in actors:
        if (
            mode == MODE_EXCEPT_OOB
            and actor.gang == "ClubDestLieut"
            and actor.name in OOB_LIEUTENANTS
        ):
            continue
        result.append(actor)
    return tuple(sorted(result, key=lambda actor: (actor.gang, actor.name)))


def _brain_addresses(
    client: PineClient, actors: tuple[Actor, ...]
) -> dict[int, tuple[Actor, int]]:
    if not actors:
        return {}
    ranges = [
        aligned_range(actor.human + HUMAN_MOVE_INDEX_OFFSET, 2)
        for actor in actors
    ]
    rows = client.read_blocks(ranges)
    result = {}
    for actor, memory_range, raw in zip(actors, ranges, rows, strict=True):
        move_index = struct.unpack_from(
            "<h",
            raw,
            actor.human + HUMAN_MOVE_INDEX_OFFSET - memory_range[0],
        )[0]
        if 0 <= move_index < 0x100:
            result[actor.handle] = (
                actor,
                BRAIN_BASE + move_index * BRAIN_STRIDE + BRAIN_DEAD_OFFSET,
            )
    return result


def _write_bytes(client: PineClient, writes: Iterable[tuple[int, int]]) -> None:
    payload = bytearray()
    for address, value in writes:
        payload.append(WRITE8)
        payload.extend(struct.pack("<I", address))
        payload.append(value & 0xFF)
    if payload:
        response = client.request(bytes(payload))
        if response:
            raise RuntimeError(
                f"respuesta inesperada al suspender IA: {len(response)} bytes"
            )


class DestroyedAIFreezeController:
    def __init__(self) -> None:
        self.mode = MODE_OFF
        # handle -> (human, brain byte address, original byte)
        self.originals: dict[int, tuple[int, int, int]] = {}

    def set_mode(
        self, client: PineClient, snapshot, mode: str
    ) -> tuple[int, int]:
        if mode not in MODES:
            raise ValueError(f"invalid AI mode: {mode}")
        restored = self.restore(client, snapshot)
        self.mode = mode
        frozen = self.enforce(client, snapshot) if mode != MODE_OFF else 0
        return frozen, restored

    def enforce(self, client: PineClient, snapshot) -> int:
        if self.mode == MODE_OFF or snapshot is None:
            return 0
        targets = select_targets(snapshot.actors.values(), mode=self.mode)
        resolved = _brain_addresses(client, targets)
        ranges = [aligned_range(address, 1) for _actor, address in resolved.values()]
        rows = client.read_blocks(ranges)
        writes = []
        for (actor, address), memory_range, raw in zip(
            resolved.values(), ranges, rows, strict=True
        ):
            current = raw[address - memory_range[0]]
            previous = self.originals.get(actor.handle)
            if previous is None or previous[:2] != (actor.human, address):
                self.originals[actor.handle] = (
                    actor.human,
                    address,
                    current,
                )
            if current != 1:
                writes.append((address, 1))
        _write_bytes(client, writes)
        return len(resolved)

    def restore(self, client: PineClient, snapshot) -> int:
        if not self.originals:
            self.mode = MODE_OFF
            return 0
        live_by_handle = snapshot.actors if snapshot is not None else {}
        writes = []
        for handle, (human, address, original) in self.originals.items():
            actor = live_by_handle.get(handle)
            if (
                actor is None
                or actor.human != human
                or actor.hp <= 1
                or client.read(human, 32) != HUMAN_VTABLE
            ):
                continue
            move_index = client.read(human + HUMAN_MOVE_INDEX_OFFSET, 16)
            if move_index & 0x8000:
                move_index -= 0x10000
            current_address = (
                BRAIN_BASE + move_index * BRAIN_STRIDE + BRAIN_DEAD_OFFSET
                if 0 <= move_index < 0x100
                else 0
            )
            if current_address == address:
                writes.append((address, original))
        _write_bytes(client, writes)
        restored = len(writes)
        self.originals.clear()
        self.mode = MODE_OFF
        return restored

    def abandon(self) -> None:
        """Forget old-world pointers after a savestate/world transition."""
        self.originals.clear()
