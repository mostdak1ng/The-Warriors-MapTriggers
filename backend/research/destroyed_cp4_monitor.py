#!/usr/bin/env python3
"""Read-only event monitor for Destroyed checkpoint 4.

Tracks the Lua ``C4.totalDestroyersDead`` value and the three gangs wired to
``ClubhouseDestroyerDied``.  The monitor never writes game memory, sends game
input, or loads/saves emulator states.

Supported game: The Warriors US v1.03 (SLUS-21215, CRC b99a75de).
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from pine_client import GAME_ID, GAME_VERSION, UUID, PineClient
from player_noclip import (
    ENTITY_REGISTRY,
    HUMAN_MOVE_INDEX_OFFSET,
    MOVE_TRANSFORM_BASE,
    MOVE_TRANSFORM_STRIDE,
    plausible_ee_pointer,
)


SUPPORTED_GAME = ("SLUS-21215", "1.03", "b99a75de")
EE_MEMORY_SIZE = 0x02000000
COUNTER_KEY = b"totalDestroyersDead\0"
LUA_TSTRING_HEADER_SIZE = 0x10
LUA_TSTRING_TAG = 3
LUA_NUMBER_TAG = 2

HUMAN_VTABLE = 0x0053F088
REGISTRY_ENTRIES = 0x4000
HUMAN_SCAN_SIZE = 0xE8
HUMAN_NAME_OFFSET = 0x80
HUMAN_CHAR_TYPE_OFFSET = 0xCC
HUMAN_CHAR_ID_OFFSET = 0xD0
HUMAN_STATE_OFFSET = 0xD4
STATE_HEALTH_OFFSET = 0x144

BRAIN_BASE = 0x006D53F0
BRAIN_STRIDE = 0x2F0
BRAIN_GANG_POINTER_OFFSET = 0x20C
GANG_NAME_OFFSET = 0x1C
GANG_NAME_SIZE = 16
GANG_MEMBER_ARRAY_OFFSET = 0x48
GANG_MEMBER_CAPACITY = 32
GANG_READ_SIZE = GANG_MEMBER_ARRAY_OFFSET + GANG_MEMBER_CAPACITY * 4
NIL_HANDLE = 0xFFFFFFFF

TARGET_GANGS = {
    "ClubhouseDestro": "fijos",
    "ClubDestLieut": "lieutenants",
    "ClubDestSpawner": "spawners",
}
SPAWNER_PREFIXES = ("ClubDestSpawn1", "ClubDestSpawn2", "ClubDestSpawn3")
SPAWNER_SEEDS = {
    "ClubDestSpawn1": (399.0, -131.7, 3.9),
    "ClubDestSpawn2": (397.0, -114.4, 3.9),
    "ClubDestSpawn3": (377.9, -117.4, 3.9),
}


@dataclass(frozen=True)
class HumanHeader:
    human: int
    handle: int
    name: str
    char_type: int
    char_id: int
    state: int
    move_index: int


@dataclass(frozen=True)
class Actor:
    handle: int
    human: int
    name: str
    gang: str
    hp: int
    max_hp: int
    char_type: int
    char_id: int
    position: tuple[float, float, float]


@dataclass(frozen=True)
class Snapshot:
    counter: int
    actors: dict[int, Actor]
    gang_counts: dict[str, int]
    emulator_status: int


def aligned_range(address: int, size: int) -> tuple[int, int]:
    base = address & ~7
    end = (address + size + 7) & ~7
    return base, end - base


def decode_fixed_name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", errors="replace")


def verify_game(client: PineClient) -> None:
    identity = (
        client.text(GAME_ID),
        client.text(GAME_VERSION),
        client.text(UUID).lower(),
    )
    if identity != SUPPORTED_GAME:
        raise RuntimeError(
            "Unsupported game: "
            f"serial={identity[0]} version={identity[1]} crc={identity[2]}"
        )


def find_all(raw: bytes, needle: bytes):
    start = 0
    while True:
        found = raw.find(needle, start)
        if found < 0:
            return
        yield found
        start = found + 1


def resolve_counter_address(client: PineClient, *, quiet: bool = False) -> int:
    """Locate the Lua table node dynamically so savestate reloads are safe."""
    if not quiet:
        print("Searching the Lua heap for C4.totalDestroyersDead...", flush=True)
    memory = client.read_block(0, EE_MEMORY_SIZE, progress=False)
    candidates: list[tuple[int, float]] = []
    for string_address in find_all(memory, COUNTER_KEY):
        if string_address < LUA_TSTRING_HEADER_SIZE:
            continue
        tstring = string_address - LUA_TSTRING_HEADER_SIZE
        pointer_bytes = struct.pack("<I", tstring)
        for key_pointer in find_all(memory, pointer_bytes):
            if key_pointer < 8 or key_pointer + 24 > len(memory):
                continue
            key_tag = struct.unpack_from("<I", memory, key_pointer - 8)[0]
            value_tag = struct.unpack_from("<I", memory, key_pointer + 8)[0]
            value = struct.unpack_from("<d", memory, key_pointer + 16)[0]
            if (
                key_tag == LUA_TSTRING_TAG
                and value_tag == LUA_NUMBER_TAG
                and math.isfinite(value)
                and 0.0 <= value <= 1000.0
                and value.is_integer()
            ):
                candidates.append((key_pointer + 16, value))
    unique = sorted(set(candidates))
    if len(unique) != 1:
        formatted = ", ".join(
            f"0x{address:08X}={value:g}" for address, value in unique
        ) or "none"
        raise RuntimeError(
            "Could not uniquely identify totalDestroyersDead "
            f"(candidates: {formatted}). Load Destroyed CP4 and try again."
        )
    address, value = unique[0]
    if not quiet:
        print(
            f"Counter found at 0x{address:08X}; "
            f"initial value={int(value)}.",
            flush=True,
        )
    return address


def read_counter(client: PineClient, address: int) -> int:
    bits = client.read(address, 64)
    value = struct.unpack("<d", struct.pack("<Q", bits))[0]
    if not math.isfinite(value) or not value.is_integer() or not 0 <= value <= 1000:
        raise ValueError(f"invalid Lua counter at 0x{address:08X}: {value!r}")
    return int(value)


def read_humans(client: PineClient) -> list[HumanHeader]:
    registry = client.read_block(
        ENTITY_REGISTRY, REGISTRY_ENTRIES * 8, progress=False
    )
    pointer_handles: dict[int, int] = {}
    for index in range(REGISTRY_ENTRIES):
        human, generation = struct.unpack_from("<IH", registry, index * 8)
        if plausible_ee_pointer(human):
            pointer_handles.setdefault(human, (index << 16) | generation)

    pointers = list(pointer_handles)
    headers = client.read_blocks([(pointer & ~7, 16) for pointer in pointers])
    human_pointers = [
        pointer
        for pointer, raw in zip(pointers, headers, strict=True)
        if struct.unpack_from("<I", raw, pointer & 7)[0] == HUMAN_VTABLE
    ]
    detail_ranges = [
        (pointer & ~7, (HUMAN_SCAN_SIZE + (pointer & 7) + 7) & ~7)
        for pointer in human_pointers
    ]
    details = client.read_blocks(detail_ranges)
    result: list[HumanHeader] = []
    for pointer, raw in zip(human_pointers, details, strict=True):
        prefix = pointer & 7
        block = raw[prefix : prefix + HUMAN_SCAN_SIZE]
        result.append(
            HumanHeader(
                human=pointer,
                handle=pointer_handles[pointer],
                name=decode_fixed_name(
                    block[HUMAN_NAME_OFFSET : HUMAN_NAME_OFFSET + 16]
                ),
                char_type=struct.unpack_from(
                    "<I", block, HUMAN_CHAR_TYPE_OFFSET
                )[0],
                char_id=struct.unpack_from("<I", block, HUMAN_CHAR_ID_OFFSET)[0],
                state=struct.unpack_from("<I", block, HUMAN_STATE_OFFSET)[0],
                move_index=struct.unpack_from(
                    "<h", block, HUMAN_MOVE_INDEX_OFFSET
                )[0],
            )
        )
    return result


def read_gang_pointers(
    client: PineClient, humans: list[HumanHeader]
) -> dict[int, int]:
    eligible = [human for human in humans if 0 <= human.move_index < 0x100]
    ranges = [
        aligned_range(
            BRAIN_BASE
            + human.move_index * BRAIN_STRIDE
            + BRAIN_GANG_POINTER_OFFSET,
            4,
        )
        for human in eligible
    ]
    rows = client.read_blocks(ranges)
    result: dict[int, int] = {}
    for human, raw, memory_range in zip(eligible, rows, ranges, strict=True):
        address = (
            BRAIN_BASE
            + human.move_index * BRAIN_STRIDE
            + BRAIN_GANG_POINTER_OFFSET
        )
        pointer = struct.unpack_from("<I", raw, address - memory_range[0])[0]
        if 0x00500000 <= pointer < EE_MEMORY_SIZE and pointer % 8 == 0:
            result[human.human] = pointer
    return result


def transform_address(human: HumanHeader) -> int:
    if 0 <= human.move_index < 0x4000:
        return MOVE_TRANSFORM_BASE + human.move_index * MOVE_TRANSFORM_STRIDE
    return human.human + 0x2C0


def read_world(client: PineClient, counter_address: int) -> Snapshot:
    counter = read_counter(client, counter_address)
    humans = read_humans(client)
    gang_pointers = read_gang_pointers(client, humans)
    unique_gangs = sorted(set(gang_pointers.values()))
    gang_rows = client.read_blocks(
        [(pointer, GANG_READ_SIZE) for pointer in unique_gangs]
    )
    gang_raw = dict(zip(unique_gangs, gang_rows, strict=True))

    gang_info: dict[int, tuple[str, set[int]]] = {}
    gang_counts = {name: 0 for name in TARGET_GANGS}
    for pointer, raw in gang_raw.items():
        name = decode_fixed_name(
            raw[GANG_NAME_OFFSET : GANG_NAME_OFFSET + GANG_NAME_SIZE]
        )
        if name not in TARGET_GANGS:
            continue
        handles = set(
            handle
            for handle in struct.unpack_from(
                f"<{GANG_MEMBER_CAPACITY}I", raw, GANG_MEMBER_ARRAY_OFFSET
            )
            if handle != NIL_HANDLE
        )
        gang_info[pointer] = (name, handles)
        gang_counts[name] = len(handles)

    relevant: list[tuple[HumanHeader, str]] = []
    for human in humans:
        gang_pointer = gang_pointers.get(human.human)
        info = gang_info.get(gang_pointer or 0)
        if info is not None and human.handle in info[1]:
            relevant.append((human, info[0]))

    state_items = [
        (human, gang, aligned_range(human.state + STATE_HEALTH_OFFSET, 4))
        for human, gang in relevant
        if plausible_ee_pointer(human.state)
    ]
    state_rows = client.read_blocks([item[2] for item in state_items])
    health: dict[int, tuple[int, int]] = {}
    for (human, _gang, memory_range), raw in zip(
        state_items, state_rows, strict=True
    ):
        health[human.handle] = struct.unpack_from(
            "<HH", raw, human.state + STATE_HEALTH_OFFSET - memory_range[0]
        )

    positioned = [item for item in relevant if item[0].handle in health]
    transform_rows = client.read_blocks(
        [(transform_address(human), 16) for human, _gang in positioned]
    )
    actors: dict[int, Actor] = {}
    for (human, gang), raw in zip(positioned, transform_rows, strict=True):
        position = struct.unpack_from("<3f", raw)
        if not all(math.isfinite(value) for value in position):
            continue
        hp, max_hp = health[human.handle]
        actors[human.handle] = Actor(
            handle=human.handle,
            human=human.human,
            name=human.name,
            gang=gang,
            hp=hp,
            max_hp=max_hp,
            char_type=human.char_type,
            char_id=human.char_id,
            position=position,
        )
    return Snapshot(counter, actors, gang_counts, client.status())


def spawner_source(name: str) -> str | None:
    for prefix in SPAWNER_PREFIXES:
        if name.startswith(prefix):
            return prefix
    return None


def event_record(kind: str, elapsed: float, counter: int, **fields) -> dict:
    return {
        "kind": kind,
        "elapsed_seconds": round(elapsed, 3),
        "counter": counter,
        **fields,
    }


def actor_fields(actor: Actor) -> dict:
    result = asdict(actor)
    result["handle"] = f"0x{actor.handle:08X}"
    result["human"] = f"0x{actor.human:08X}"
    result["position"] = [round(value, 3) for value in actor.position]
    source = spawner_source(actor.name)
    if source:
        result["spawner_source"] = source
    return result


def print_actor(prefix: str, actor: Actor, counter: int) -> None:
    x, y, z = actor.position
    print(
        f"{prefix:<9} C={counter:02d} {actor.gang:<16} "
        f"{actor.name:<16} HP={actor.hp:4d}/{actor.max_hp:<4d} "
        f"handle=0x{actor.handle:08X} pos=({x:.1f},{y:.1f},{z:.1f})",
        flush=True,
    )


def print_snapshot(snapshot: Snapshot, counter_address: int) -> None:
    statuses = {0: "running", 1: "paused", 2: "off"}
    print(
        f"\nC4.totalDestroyersDead={snapshot.counter} "
        f"(0x{counter_address:08X}) | "
        f"PCSX2={statuses.get(snapshot.emulator_status, 'unknown')}"
    )
    print("Gangs connected to the callback:")
    for gang, label in TARGET_GANGS.items():
        print(f"  {gang:<16} {snapshot.gang_counts.get(gang, 0):2d}  {label}")
    print("Current actors:")
    for actor in sorted(snapshot.actors.values(), key=lambda item: (item.gang, item.name)):
        print_actor("PRESENT", actor, snapshot.counter)
    spawner_counts = {prefix: 0 for prefix in SPAWNER_PREFIXES}
    for actor in snapshot.actors.values():
        source = spawner_source(actor.name)
        if source:
            spawner_counts[source] += 1
    print("Active spawners:")
    for prefix, seed in SPAWNER_SEEDS.items():
        print(f"  {prefix}: {spawner_counts[prefix]} | seed={seed}")


class JsonlLog:
    def __init__(self, path: Path | None):
        self.path = path
        self.file = None

    def __enter__(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("w", encoding="utf-8")
        return self

    def write(self, record: dict) -> None:
        if self.file is not None:
            self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.file.flush()

    def __exit__(self, exc_type, exc, traceback):
        if self.file is not None:
            self.file.close()


def default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "captures" / f"destroyed_cp4_{stamp}.jsonl"


def monitor(client: PineClient, interval: float, log_path: Path | None) -> None:
    counter_address = resolve_counter_address(client)
    previous = read_world(client, counter_address)
    start = time.monotonic()
    print_snapshot(previous, counter_address)
    print("\nMONITORING. Continue playing; Ctrl+C stops and preserves the log.\n")

    with JsonlLog(log_path) as log:
        log.write(
            event_record(
                "baseline",
                0.0,
                previous.counter,
                counter_address=f"0x{counter_address:08X}",
                gang_counts=previous.gang_counts,
                actors=[actor_fields(actor) for actor in previous.actors.values()],
            )
        )
        last_status = start
        while True:
            loop_start = time.monotonic()
            elapsed = loop_start - start
            try:
                current = read_world(client, counter_address)
            except (ConnectionError, OSError, RuntimeError, ValueError) as error:
                print(f"READ      t={elapsed:7.2f}s {error}", flush=True)
                log.write(event_record("read_error", elapsed, previous.counter, error=str(error)))
                time.sleep(max(interval, 0.5))
                continue

            if current.counter < previous.counter:
                print(
                    f"RELOAD?   t={elapsed:7.2f}s counter "
                    f"{previous.counter}->{current.counter}; resetting baseline",
                    flush=True,
                )
                log.write(
                    event_record(
                        "counter_reset",
                        elapsed,
                        current.counter,
                        previous_counter=previous.counter,
                    )
                )
                previous = current
                continue

            if current.counter != previous.counter:
                delta = current.counter - previous.counter
                print(
                    f"CALLBACK  t={elapsed:7.2f}s counter "
                    f"{previous.counter}->{current.counter} (delta +{delta})",
                    flush=True,
                )
                log.write(
                    event_record(
                        "counter_change",
                        elapsed,
                        current.counter,
                        previous_counter=previous.counter,
                        delta=delta,
                    )
                )

            old_handles = set(previous.actors)
            new_handles = set(current.actors)
            for handle in sorted(new_handles - old_handles):
                actor = current.actors[handle]
                print_actor("ADDED", actor, current.counter)
                log.write(
                    event_record(
                        "actor_added", elapsed, current.counter, actor=actor_fields(actor)
                    )
                )
            for handle in sorted(old_handles & new_handles):
                old = previous.actors[handle]
                new = current.actors[handle]
                # Defeated humans in this build normally remain at 1 HP while
                # their death lifecycle finishes; waiting for literal zero
                # misses the event.
                if old.hp > 1 and new.hp <= 1:
                    print_actor("DEFEATED", new, current.counter)
                    log.write(
                        event_record(
                            "defeated", elapsed, current.counter, actor=actor_fields(new)
                        )
                    )
            for handle in sorted(old_handles - new_handles):
                actor = previous.actors[handle]
                if previous.counter >= 30:
                    kind = "CLEANUP"
                elif actor.hp <= 1:
                    kind = "REMOVED"
                else:
                    kind = "EXIT"
                print_actor(kind, actor, current.counter)
                log.write(
                    event_record(
                        "actor_removed",
                        elapsed,
                        current.counter,
                        removal=kind.lower(),
                        actor=actor_fields(actor),
                    )
                )

            if current.gang_counts != previous.gang_counts:
                print(
                    f"GANG      t={elapsed:7.2f}s {current.gang_counts}",
                    flush=True,
                )
                log.write(
                    event_record(
                        "gang_counts",
                        elapsed,
                        current.counter,
                        gang_counts=current.gang_counts,
                    )
                )

            if loop_start - last_status >= 5.0:
                statuses = {0: "RUN", 1: "PAUSE", 2: "OFF"}
                print(
                    f"STATUS    t={elapsed:7.2f}s C={current.counter:02d} "
                    f"actors={len(current.actors):02d} "
                    f"PCSX2={statuses.get(current.emulator_status, '?')}",
                    flush=True,
                )
                last_status = loop_start

            previous = current
            if current.counter >= 30 and not current.actors:
                print(
                    "CAPTURE  gate=30 reached and gangs empty; stopping automatically.",
                    flush=True,
                )
                log.write(event_record("capture_complete", elapsed, current.counter))
                return
            delay = interval - (time.monotonic() - loop_start)
            if delay > 0:
                time.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only monitor for Destroyed, checkpoint 4."
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument(
        "--interval",
        type=float,
        default=0.10,
        help="seconds between reads (default: 0.10)",
    )
    parser.add_argument(
        "--once", action="store_true", help="print a snapshot and exit"
    )
    parser.add_argument(
        "--no-log", action="store_true", help="disable JSONL capture"
    )
    parser.add_argument("--log", type=Path, help="alternative JSONL path")
    args = parser.parse_args()
    if args.interval < 0.05:
        parser.error("--interval must be >= 0.05")

    client: PineClient | None = None
    try:
        client = PineClient(port=args.port)
        verify_game(client)
        if args.once:
            counter_address = resolve_counter_address(client)
            print_snapshot(read_world(client, counter_address), counter_address)
            return
        log_path = None if args.no_log else (args.log or default_log_path())
        if log_path is not None:
            print(f"Capture will be saved to: {log_path.resolve()}")
        monitor(client, args.interval, log_path)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"ERROR: {error}")
        print(
            "Close MapTriggers first if it is running: PCSX2 serves only "
            "one PINE session at a time."
        )
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
