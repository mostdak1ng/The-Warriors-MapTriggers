#!/usr/bin/env python3
"""Compact Destroyed CP4 state transported by freecam's UDP feed.

The sampler is called by ``freecam_probe.py``, which already owns PCSX2's only
PINE session.  The overlay decodes these packets without opening PINE.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from destroyed_cp4_monitor import Actor, read_world, resolve_counter_address
from pine_client import PineClient


MAGIC = b"TWDC"
VERSION = 1
FLAG_AVAILABLE = 0x01
FLAG_AI_FREEZE_ALL = 0x02
FLAG_AI_FREEZE_EXCEPT_OOB = 0x04

AI_MODE_OFF = "off"
AI_MODE_ALL = "all"
AI_MODE_EXCEPT_OOB = "except_oob"

# magic, version, flags, counter, three native gang counts, actor count
HEADER = struct.Struct("<4sBBh4H")
# handle, gang id, reserved, hp/max hp, XYZ and fixed actor name
ACTOR_RECORD = struct.Struct("<IBB2xHH3f16s")

GANG_IDS = {
    "ClubhouseDestro": 1,
    "ClubDestLieut": 2,
    "ClubDestSpawner": 3,
}
GANG_NAMES = {value: key for key, value in GANG_IDS.items()}
GANG_ORDER = tuple(GANG_IDS)


@dataclass(frozen=True)
class TelemetryActor:
    handle: int
    gang_id: int
    name: str
    hp: int
    max_hp: int
    position: tuple[float, float, float]


@dataclass(frozen=True)
class TelemetrySnapshot:
    available: bool
    counter: int
    gang_counts: tuple[int, int, int]
    actors: tuple[TelemetryActor, ...]
    ai_mode: str


def _fixed_name(name: str) -> bytes:
    return name.encode("ascii", errors="replace")[:15].ljust(16, b"\0")


def encode_unavailable() -> bytes:
    return HEADER.pack(MAGIC, VERSION, 0, -1, 0, 0, 0, 0)


def encode_actor(actor: Actor) -> bytes:
    return ACTOR_RECORD.pack(
        actor.handle,
        GANG_IDS[actor.gang],
        0,
        actor.hp,
        actor.max_hp,
        *actor.position,
        _fixed_name(actor.name),
    )


def encode_snapshot(
    counter: int,
    gang_counts: dict[str, int],
    actors,
    *,
    ai_mode: str = AI_MODE_OFF,
) -> bytes:
    ordered = tuple(sorted(actors, key=lambda actor: (actor.gang, actor.name)))
    counts = tuple(gang_counts.get(gang, 0) for gang in GANG_ORDER)
    flags = FLAG_AVAILABLE
    if ai_mode == AI_MODE_ALL:
        flags |= FLAG_AI_FREEZE_ALL
    elif ai_mode == AI_MODE_EXCEPT_OOB:
        flags |= FLAG_AI_FREEZE_EXCEPT_OOB
    elif ai_mode != AI_MODE_OFF:
        raise ValueError(f"invalid AI mode: {ai_mode}")
    return HEADER.pack(
        MAGIC,
        VERSION,
        flags,
        counter,
        *counts,
        len(ordered),
    ) + b"".join(encode_actor(actor) for actor in ordered)


def decode_packet(packet: bytes) -> TelemetrySnapshot | None:
    offset = packet.find(MAGIC, 0x28)
    if offset < 0 or offset + HEADER.size > len(packet):
        return None
    (
        magic,
        version,
        flags,
        counter,
        fixed_count,
        lieutenant_count,
        spawner_count,
        actor_count,
    ) = HEADER.unpack_from(packet, offset)
    if magic != MAGIC or version != VERSION or actor_count > 64:
        return None
    records_start = offset + HEADER.size
    records_end = records_start + actor_count * ACTOR_RECORD.size
    if records_end > len(packet):
        return None
    actors: list[TelemetryActor] = []
    for index in range(actor_count):
        values = ACTOR_RECORD.unpack_from(
            packet, records_start + index * ACTOR_RECORD.size
        )
        name = values[8].split(b"\0", 1)[0].decode("ascii", errors="replace")
        if values[1] not in GANG_NAMES:
            continue
        actors.append(
            TelemetryActor(
                handle=values[0],
                gang_id=values[1],
                hp=values[3],
                max_hp=values[4],
                position=(values[5], values[6], values[7]),
                name=name,
            )
        )
    if flags & FLAG_AI_FREEZE_ALL:
        ai_mode = AI_MODE_ALL
    elif flags & FLAG_AI_FREEZE_EXCEPT_OOB:
        ai_mode = AI_MODE_EXCEPT_OOB
    else:
        ai_mode = AI_MODE_OFF
    return TelemetrySnapshot(
        available=bool(flags & FLAG_AVAILABLE),
        counter=counter,
        gang_counts=(fixed_count, lieutenant_count, spawner_count),
        actors=tuple(actors),
        ai_mode=ai_mode,
    )


class DestroyedCP4TelemetrySampler:
    """Resolve Lua state and produce a cached, compact UDP suffix."""

    def __init__(self, client: PineClient) -> None:
        self.counter_address: int | None = None
        self.latest_snapshot = None
        self.ai_freeze_mode = AI_MODE_OFF
        self.next_resolve = 0.0
        self.last_error = ""
        self._resolve(client)

    def _report(self, message: str) -> None:
        if message != self.last_error:
            print(message, flush=True)
            self.last_error = message

    def _resolve(self, client: PineClient) -> bool:
        try:
            self.counter_address = resolve_counter_address(client, quiet=True)
        except (RuntimeError, ValueError, struct.error) as error:
            self.counter_address = None
            self.latest_snapshot = None
            self.next_resolve = time.monotonic() + 2.0
            self._report(f"DESTROYED CP4 telemetry waiting for counter: {error}")
            return False
        self.last_error = ""
        print(
            "DESTROYED CP4 telemetry active: "
            f"totalDestroyersDead=0x{self.counter_address:08X}",
            flush=True,
        )
        return True

    def sample(self, client: PineClient) -> bytes:
        if self.counter_address is None:
            if time.monotonic() < self.next_resolve or not self._resolve(client):
                return encode_unavailable()
        assert self.counter_address is not None
        try:
            snapshot = read_world(client, self.counter_address)
        except (RuntimeError, ValueError, struct.error) as error:
            self.counter_address = None
            self.latest_snapshot = None
            self.next_resolve = time.monotonic() + 0.5
            self._report(f"DESTROYED CP4 telemetry resolving again: {error}")
            return encode_unavailable()
        self.last_error = ""
        self.latest_snapshot = snapshot
        return encode_snapshot(
            snapshot.counter,
            snapshot.gang_counts,
            snapshot.actors.values(),
            ai_mode=self.ai_freeze_mode,
        )

    def set_ai_freeze_mode(self, mode: str) -> None:
        if mode not in {AI_MODE_OFF, AI_MODE_ALL, AI_MODE_EXCEPT_OOB}:
            raise ValueError(f"invalid AI mode: {mode}")
        self.ai_freeze_mode = mode
