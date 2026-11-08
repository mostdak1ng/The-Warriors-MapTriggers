#!/usr/bin/env python3
"""Generic native-gang and gang-spawner HUD for The Warriors.

The HUD labels every loaded Human except members of the Warriors gang.  It
also reads the four native spawner slots owned by every active gang and the
engine-wide current/max spawner counters.  Game memory is never written.

Supported game: The Warriors US v1.03 (SLUS-21215, CRC b99a75de).
"""

from __future__ import annotations

import argparse
import colorsys
import ctypes
import json
import math
import socket
import struct
import threading
import time
import tkinter as tk
import zlib
from dataclasses import dataclass, replace

import payin_gang_overlay as base
from pine_client import PineClient
from player_noclip import plausible_ee_pointer


GANG_POOL_BASE = 0x005E6E30
GANG_POOL_STRIDE = 0xB10
GANG_POOL_CAPACITY = 32
GANG_ACTIVE_OFFSET = 0x18

SPAWNER_BASE_OFFSET = 0x640
SPAWNER_STRIDE = 0x130
SPAWNER_CAPACITY = 4
SPAWNER_POSITION_OFFSET = 0x00
SPAWNER_NAME_POINTER_OFFSET = 0x14
SPAWNER_CONFIGURED_OFFSET = 0x50
SPAWNER_MODE_OFFSET = 0x52
SPAWNER_TARGET_OFFSET = 0x5A
SPAWNER_CURRENT_OFFSET = 0x5C
SPAWNER_MUST_BE_OFFSCREEN_OFFSET = 0x8C

# SetSpawnMax writes a halfword at [0x0051489C] + 0x434.  Successful gang
# spawns increment the adjacent halfword and the spawner cleanup path
# decrements it, making +0x432 the exact engine-wide live spawner count.
GAME_MANAGER_POINTER = 0x0051489C
SPAWNER_GLOBAL_CURRENT_OFFSET = 0x432
SPAWNER_GLOBAL_MAX_OFFSET = 0x434

NO_GANG = "SIN GANG"
WARRIORS_NAMES = {"warriors", "gangwarriors"}
WANDERMOON_TOKEN = "wandermoon"
SINGLE_INSTANCE_MUTEX = "Local\\TheWarriors_AllGangOverlay"
PACKET_MAGIC = b"AGNG1"

# Per-Human AI state.  These are the same native channels used by the
# dedicated AI monitors: State + 0xCC is the target of the current action,
# while Brain + 0x260 is the longer-lived tactical target.
STATE_ACTION_TARGET_OFFSET = 0xCC
BRAIN_TOP_GOAL_OFFSET = 0x2C
BRAIN_GOAL_STACK_OFFSET = 0x40
BRAIN_GOAL_STACK_CAPACITY = 16
BRAIN_TACTICAL_TARGET_OFFSET = 0x260
BRAIN_DESTINATION_READ_SIZE = BRAIN_TACTICAL_TARGET_OFFSET + 4
GOAL_READ_SIZE = 0x30
GOAL_VTABLE_OFFSET = 0x0C
GOAL_DESTINATION_OFFSET = 0x10
GOAL_HUMAN_HANDLE_OFFSET = 0x20

# Goal vtables from SLUS-21215 v1.03.  MoveToPosition is the child emitted by
# Wander.  Wander also retains the last selected point at +0x10 while a
# transient child goal (for example MoveToUseFlag) is on top of the stack.
GOAL_VTABLE_WANDER = 0x00541A70
GOAL_VTABLE_HANG_OUT = 0x00541650
GOAL_VTABLE_HOLD_POSITION = 0x00540510
GOAL_VTABLE_MOVE_TO_HUMAN = 0x00541F50
GOAL_VTABLE_MOVE_TO_POSITION = 0x00541FB0
GOAL_VTABLE_MOVE_TO_FLAG_NET_FLAG = 0x00542070
GOAL_VTABLE_MOVE_TO_FLAG = 0x00542130
POSITION_GOALS = {
    GOAL_VTABLE_HANG_OUT: "HANGOUT",
    GOAL_VTABLE_HOLD_POSITION: "MANTIENE POS",
    GOAL_VTABLE_MOVE_TO_POSITION: "WANDER",
    GOAL_VTABLE_MOVE_TO_FLAG_NET_FLAG: "VA A FLAG",
    GOAL_VTABLE_MOVE_TO_FLAG: "VA A FLAG",
}

PANEL_BG = "#0d151e"
LABEL_BG = "#0a1118"
PANEL_BORDER = "#4ac9ed"
TEXT = "#f3f8fb"
DIM = "#91a3ad"
CYAN = "#65dcff"
GREEN = "#71f59b"
YELLOW = "#ffd166"
RED = "#ff7474"

CameraReading = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    float,
    float,
]


@dataclass(frozen=True)
class SpawnerReading:
    slot: int
    name: str
    mode: int
    target: int
    current: int
    must_be_offscreen: bool
    position: tuple[float, float, float]
    camera_blocked: bool | None

    @property
    def running(self) -> bool:
        return self.mode != 0


@dataclass(frozen=True)
class GangReading:
    pointer: int
    name: str
    label: str
    color: str
    remaining: int
    members: tuple[int, ...]
    spawners: tuple[SpawnerReading, ...] = ()


@dataclass(frozen=True)
class ActorReading:
    human: int
    handle: int
    actor_name: str
    gang_name: str
    gang_label: str
    gang_color: str
    remaining: int
    hp: int
    max_hp: int
    position: tuple[float, float, float]
    ai_goal: str = ""
    destination_kind: str = ""
    destination_name: str = ""
    destination_position: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class WorldSnapshot:
    message: str = "Connecting to PCSX2..."
    enemies: tuple[ActorReading, ...] = ()
    gangs: tuple[GangReading, ...] = ()
    camera: CameraReading | None = None
    spawner_current: int | None = None
    spawner_max: int | None = None
    reconnects: int = 0


def acquire_single_instance() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    )
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = int(kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX) or 0)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return 0
    return handle


def release_single_instance(handle: int) -> None:
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def plausible_string_pointer(pointer: int) -> bool:
    return 0x00100000 <= pointer < 0x02000000


def gang_label(name: str) -> str:
    return name[4:] if name.startswith("Gang") and len(name) > 4 else name


def gang_color(name: str) -> str:
    """Return a bright deterministic color, stable between processes."""
    hue = (zlib.crc32(name.encode("utf-8")) & 0xFFFF) / 65536.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.62, 1.0)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def is_warriors(name: str) -> bool:
    return name.casefold() in WARRIORS_NAMES


def is_wandermoon(name: str) -> bool:
    return WANDERMOON_TOKEN in name.casefold()


def decode_goal_pointers(brain_raw: bytes) -> tuple[int, ...]:
    if len(brain_raw) < BRAIN_GOAL_STACK_OFFSET:
        return ()
    top = struct.unpack_from("<b", brain_raw, BRAIN_TOP_GOAL_OFFSET)[0]
    if top < 0:
        return ()
    top = min(top, BRAIN_GOAL_STACK_CAPACITY - 1)
    required = BRAIN_GOAL_STACK_OFFSET + (top + 1) * 4
    if len(brain_raw) < required:
        return ()
    return tuple(
        pointer
        for pointer in struct.unpack_from(
            f"<{top + 1}I",
            brain_raw,
            BRAIN_GOAL_STACK_OFFSET,
        )
        if plausible_ee_pointer(pointer)
    )


def decode_goal_position(raw: bytes) -> tuple[float, float, float] | None:
    if len(raw) < GOAL_DESTINATION_OFFSET + 12:
        return None
    position = struct.unpack_from("<3f", raw, GOAL_DESTINATION_OFFSET)
    if not all(
        math.isfinite(component) and abs(component) < 100_000.0
        for component in position
    ):
        return None
    return tuple(position)


def resolve_wandermoon_destination(
    brain_raw: bytes,
    goal_rows: dict[int, bytes],
    action_target: int,
    handle_names: dict[int, str],
) -> tuple[str, str, str, tuple[float, float, float] | None]:
    """Resolve the live target selected by one WanderMoon Human.

    Returns ``(goal, kind, target_name, position)``.  Actor targets take
    precedence over navigation goals because combat/pursuit can leave an
    older Wander destination in the parent goal object.
    """
    action_name = handle_names.get(action_target)
    if action_name is not None:
        return "ACTION", "attack", action_name, None

    tactical_target = base.NIL_HANDLE
    if len(brain_raw) >= BRAIN_TACTICAL_TARGET_OFFSET + 4:
        tactical_target = struct.unpack_from(
            "<I", brain_raw, BRAIN_TACTICAL_TARGET_OFFSET
        )[0]
    tactical_name = handle_names.get(tactical_target)
    if tactical_name is not None:
        return "TACTICAL", "pursuit", tactical_name, None

    pointers = decode_goal_pointers(brain_raw)
    for pointer in reversed(pointers):
        raw = goal_rows.get(pointer, b"")
        if len(raw) < GOAL_HUMAN_HANDLE_OFFSET + 4:
            continue
        vtable = struct.unpack_from("<I", raw, GOAL_VTABLE_OFFSET)[0]
        if vtable != GOAL_VTABLE_MOVE_TO_HUMAN:
            continue
        target_handle = struct.unpack_from(
            "<I", raw, GOAL_HUMAN_HANDLE_OFFSET
        )[0]
        target_name = handle_names.get(target_handle)
        if target_name is not None:
            return "MOVE TO HUMAN", "pursuit", target_name, None

    # Prefer the active child.  If Wander temporarily pushed another child,
    # fall back to the parent, which retains its last chosen point at +0x10.
    wander_fallback: tuple[float, float, float] | None = None
    for pointer in reversed(pointers):
        raw = goal_rows.get(pointer, b"")
        if len(raw) < GOAL_VTABLE_OFFSET + 4:
            continue
        vtable = struct.unpack_from("<I", raw, GOAL_VTABLE_OFFSET)[0]
        position = decode_goal_position(raw)
        if position is None:
            continue
        goal_name = POSITION_GOALS.get(vtable)
        if goal_name is not None:
            return goal_name, "position", "", position
        if vtable == GOAL_VTABLE_WANDER:
            wander_fallback = position
    if wander_fallback is not None:
        return "WANDER", "position", "", wander_fallback
    return "", "unknown", "", None


def destination_text(actor: ActorReading) -> str:
    if not is_wandermoon(actor.gang_name):
        return ""
    if actor.destination_kind == "attack" and actor.destination_name:
        return f"ATACA -> {actor.destination_name}"
    if actor.destination_kind == "pursuit" and actor.destination_name:
        return f"PERSIGUE -> {actor.destination_name}"
    if actor.destination_position is not None:
        x, y, z = actor.destination_position
        prefix = actor.ai_goal or "DESTINO"
        return f"{prefix} -> ({x:.1f}, {y:.1f}, {z:.1f})"
    return "DESTINO -> SIN ASIGNAR"


def decode_member_handles(raw: bytes) -> tuple[int, ...]:
    handles = struct.unpack_from(
        f"<{base.GANG_MEMBER_CAPACITY}I",
        raw,
        base.GANG_MEMBER_ARRAY_OFFSET,
    )
    return tuple(handle for handle in handles if handle != base.NIL_HANDLE)


def camera_sees_spawn_candidate(
    camera: CameraReading | None,
    position: tuple[float, float, float],
    margin: float = 0.3,
) -> bool | None:
    """Approximate the native off-screen gate for the resolved camera.

    The game tests candidate Z + 1.6 and supplies 0.3 to its visibility helper.
    Native split-screen checks every active camera; this HUD only has the
    currently resolved camera, so a False result means free in *this* camera.
    """
    if camera is None:
        return None
    origin, right, forward, up, horizontal_slope, vertical_slope = camera
    candidate = (position[0], position[1], position[2] + 1.6)
    relative = tuple(candidate[index] - origin[index] for index in range(3))
    x = base.GangOverlay.dot(relative, right)
    depth = base.GangOverlay.dot(relative, forward)
    z = base.GangOverlay.dot(relative, up)
    if depth <= 0.08:
        return False
    return (
        abs(x / depth) <= horizontal_slope + margin
        and abs(z / depth) <= vertical_slope + margin
    )


def decode_engine_spawn_counts(raw: bytes) -> tuple[int, int]:
    if len(raw) < 4:
        raise ValueError("truncated global spawner counter")
    current, maximum = struct.unpack_from("<HH", raw)
    if maximum > 4096 or current > 4096:
        raise ValueError("invalid global spawner counter")
    return current, maximum


def spawner_gate_text(spawner: SpawnerReading) -> str:
    prefix = f"{spawner.name} M{spawner.mode}"
    if not spawner.running:
        return f"{prefix} · STOPPED"
    if not spawner.must_be_offscreen:
        return f"{prefix} · NO CAMERA RESTRICTION"
    if spawner.camera_blocked is None:
        return f"{prefix} · OFFSCREEN: CAMERA ?"
    if spawner.camera_blocked:
        return f"{prefix} · OFFSCREEN: CAMERA BLOCKS*"
    return f"{prefix} · OFFSCREEN: CLEAR FROM CAMERA*"


def snapshot_to_packet(snapshot: WorldSnapshot) -> bytes:
    """Encode a gang snapshot for the single-PINE-client UDP bridge."""
    payload = {
        "message": snapshot.message,
        "camera": snapshot.camera,
        "spawner_current": snapshot.spawner_current,
        "spawner_max": snapshot.spawner_max,
        "gangs": [
            [
                gang.pointer,
                gang.name,
                gang.label,
                gang.color,
                gang.remaining,
                [
                    [
                        spawner.slot,
                        spawner.name,
                        spawner.mode,
                        spawner.target,
                        spawner.current,
                        int(spawner.must_be_offscreen),
                        spawner.position,
                        spawner.camera_blocked,
                    ]
                    for spawner in gang.spawners
                ],
            ]
            for gang in snapshot.gangs
        ],
        "actors": [
            [
                actor.human,
                actor.handle,
                actor.actor_name,
                actor.gang_name,
                actor.gang_label,
                actor.gang_color,
                actor.remaining,
                actor.hp,
                actor.max_hp,
                actor.position,
                actor.ai_goal,
                actor.destination_kind,
                actor.destination_name,
                actor.destination_position,
            ]
            for actor in snapshot.enemies
        ],
    }
    packet = PACKET_MAGIC + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(packet) > 65507:
        raise ValueError(f"snapshot UDP demasiado grande: {len(packet)} bytes")
    return packet


def snapshot_from_packet(packet: bytes) -> WorldSnapshot:
    if not packet.startswith(PACKET_MAGIC):
        raise ValueError("invalid gang telemetry signature")
    payload = json.loads(packet[len(PACKET_MAGIC) :].decode("utf-8"))
    camera_payload = payload.get("camera")
    camera: CameraReading | None = None
    if camera_payload is not None:
        if len(camera_payload) != 6:
            raise ValueError("truncated camera telemetry")
        camera = (
            tuple(camera_payload[0]),
            tuple(camera_payload[1]),
            tuple(camera_payload[2]),
            tuple(camera_payload[3]),
            float(camera_payload[4]),
            float(camera_payload[5]),
        )
    gangs: list[GangReading] = []
    for row in payload.get("gangs", []):
        spawners = tuple(
            SpawnerReading(
                slot=int(item[0]),
                name=str(item[1]),
                mode=int(item[2]),
                target=int(item[3]),
                current=int(item[4]),
                must_be_offscreen=bool(item[5]),
                position=tuple(item[6]),
                camera_blocked=(
                    None if item[7] is None else bool(item[7])
                ),
            )
            for item in row[5]
        )
        gangs.append(
            GangReading(
                pointer=int(row[0]),
                name=str(row[1]),
                label=str(row[2]),
                color=str(row[3]),
                remaining=int(row[4]),
                members=(),
                spawners=spawners,
            )
        )
    actors = tuple(
        ActorReading(
            human=int(row[0]),
            handle=int(row[1]),
            actor_name=str(row[2]),
            gang_name=str(row[3]),
            gang_label=str(row[4]),
            gang_color=str(row[5]),
            remaining=int(row[6]),
            hp=int(row[7]),
            max_hp=int(row[8]),
            position=tuple(row[9]),
            ai_goal=str(row[10]) if len(row) > 10 else "",
            destination_kind=str(row[11]) if len(row) > 11 else "",
            destination_name=str(row[12]) if len(row) > 12 else "",
            destination_position=(
                tuple(row[13])
                if len(row) > 13 and row[13] is not None
                else None
            ),
        )
        for row in payload.get("actors", [])
    )
    return WorldSnapshot(
        message=str(payload.get("message", "ONLINE · UDP telemetry")),
        enemies=actors,
        gangs=tuple(gangs),
        camera=camera,
        spawner_current=payload.get("spawner_current"),
        spawner_max=payload.get("spawner_max"),
    )


class AllGangReader:
    @staticmethod
    def read_spawner_names(
        client: PineClient,
        pointers: list[int],
    ) -> dict[int, str]:
        unique = sorted({pointer for pointer in pointers if plausible_string_pointer(pointer)})
        if not unique:
            return {}
        ranges = [base.aligned_range(pointer, 32) for pointer in unique]
        rows = client.read_blocks(ranges)
        result: dict[int, str] = {}
        for pointer, memory_range, raw in zip(unique, ranges, rows, strict=True):
            offset = pointer - memory_range[0]
            value = base.decode_fixed_name(raw[offset : offset + 32])
            if value and all(character.isprintable() for character in value):
                result[pointer] = value
        return result

    @staticmethod
    def parse_gang_rows(
        raw: bytes,
        spawner_names: dict[int, str],
        camera: CameraReading | None,
    ) -> dict[int, GangReading]:
        result: dict[int, GangReading] = {}
        for index in range(GANG_POOL_CAPACITY):
            pointer = GANG_POOL_BASE + index * GANG_POOL_STRIDE
            start = index * GANG_POOL_STRIDE
            row = raw[start : start + GANG_POOL_STRIDE]
            if len(row) != GANG_POOL_STRIDE:
                continue
            if struct.unpack_from("<I", row, GANG_ACTIVE_OFFSET)[0] == 0:
                continue
            name = base.decode_fixed_name(
                row[
                    base.GANG_NAME_OFFSET :
                    base.GANG_NAME_OFFSET + base.GANG_NAME_SIZE
                ]
            )
            if not name or not all(character.isprintable() for character in name):
                continue
            members = decode_member_handles(row)
            spawners: list[SpawnerReading] = []
            for slot in range(SPAWNER_CAPACITY):
                offset = SPAWNER_BASE_OFFSET + slot * SPAWNER_STRIDE
                if row[offset + SPAWNER_CONFIGURED_OFFSET] == 0:
                    continue
                name_pointer = struct.unpack_from(
                    "<I", row, offset + SPAWNER_NAME_POINTER_OFFSET
                )[0]
                position = struct.unpack_from(
                    "<3f", row, offset + SPAWNER_POSITION_OFFSET
                )
                if not all(math.isfinite(component) for component in position):
                    position = (0.0, 0.0, 0.0)
                mode = struct.unpack_from("<h", row, offset + SPAWNER_MODE_OFFSET)[0]
                target = struct.unpack_from("<h", row, offset + SPAWNER_TARGET_OFFSET)[0]
                current = struct.unpack_from("<h", row, offset + SPAWNER_CURRENT_OFFSET)[0]
                must_be_offscreen = bool(struct.unpack_from(
                    "<I", row, offset + SPAWNER_MUST_BE_OFFSCREEN_OFFSET
                )[0])
                spawners.append(
                    SpawnerReading(
                        slot=slot,
                        name=spawner_names.get(name_pointer, f"slot{slot + 1}"),
                        mode=mode,
                        target=target,
                        current=current,
                        must_be_offscreen=must_be_offscreen,
                        position=tuple(position),
                        camera_blocked=(
                            camera_sees_spawn_candidate(camera, tuple(position))
                            if must_be_offscreen and mode != 0
                            else None
                        ),
                    )
                )
            result[pointer] = GangReading(
                pointer=pointer,
                name=name,
                label=gang_label(name),
                color=gang_color(name),
                remaining=len(members),
                members=members,
                spawners=tuple(spawners),
            )
        return result

    @staticmethod
    def read_global_counts(client: PineClient) -> tuple[int | None, int | None]:
        try:
            manager = struct.unpack(
                "<I", client.read_block(GAME_MANAGER_POINTER, 4, progress=False)
            )[0]
            if not plausible_ee_pointer(manager):
                return None, None
            raw = client.read_block(
                manager + SPAWNER_GLOBAL_CURRENT_OFFSET,
                4,
                progress=False,
            )
            return decode_engine_spawn_counts(raw)
        except (RuntimeError, ValueError, struct.error):
            return None, None

    def read(
        self,
        client: PineClient,
        *,
        camera_override: CameraReading | None = None,
    ) -> WorldSnapshot:
        humans = base.WorldReader.read_humans(client)
        brain_gangs = base.WorldReader.read_gang_pointers(client, humans)

        camera = camera_override
        if camera is None:
            try:
                camera_transform = base.resolve_camera_transform(client)
                camera_raw = client.read_block(camera_transform, 0x60, progress=False)
                camera = base.decode_camera(camera_raw)
            except (RuntimeError, ValueError, struct.error):
                camera = None

        pool_raw = client.read_block(
            GANG_POOL_BASE,
            GANG_POOL_STRIDE * GANG_POOL_CAPACITY,
            progress=False,
        )
        name_pointers: list[int] = []
        for index in range(GANG_POOL_CAPACITY):
            row_start = index * GANG_POOL_STRIDE
            row = pool_raw[row_start : row_start + GANG_POOL_STRIDE]
            if (
                len(row) != GANG_POOL_STRIDE
                or struct.unpack_from("<I", row, GANG_ACTIVE_OFFSET)[0] == 0
            ):
                continue
            for slot in range(SPAWNER_CAPACITY):
                offset = SPAWNER_BASE_OFFSET + slot * SPAWNER_STRIDE
                if row[offset + SPAWNER_CONFIGURED_OFFSET]:
                    name_pointers.append(struct.unpack_from(
                        "<I", row, offset + SPAWNER_NAME_POINTER_OFFSET
                    )[0])
        spawner_names = self.read_spawner_names(client, name_pointers)
        gangs_by_pointer = self.parse_gang_rows(pool_raw, spawner_names, camera)

        gang_by_member: dict[int, GangReading] = {}
        for gang in gangs_by_pointer.values():
            for handle in gang.members:
                gang_by_member.setdefault(handle, gang)

        relevant: list[tuple[base.HumanMeta, GangReading | None]] = []
        for human in humans:
            gang = gangs_by_pointer.get(brain_gangs.get(human.human, 0))
            if gang is None or human.handle not in gang.members:
                gang = gang_by_member.get(human.handle)
            if gang is not None and is_warriors(gang.name):
                continue
            relevant.append((human, gang))

        state_items: list[
            tuple[base.HumanMeta, GangReading | None, tuple[int, int]]
        ] = []
        for human, gang in relevant:
            if not plausible_ee_pointer(human.state):
                continue
            state_start = human.state + base.STATE_HEALTH_OFFSET
            if gang is not None and is_wandermoon(gang.name):
                state_start = human.state + STATE_ACTION_TARGET_OFFSET
            state_end = human.state + base.STATE_HEALTH_OFFSET + 4
            state_items.append(
                (human, gang, base.aligned_range(state_start, state_end - state_start))
            )
        state_rows = client.read_blocks([item[2] for item in state_items]) if state_items else []
        health: dict[int, tuple[int, int]] = {}
        action_targets: dict[int, int] = {}
        for (human, gang, memory_range), raw in zip(
            state_items, state_rows, strict=True
        ):
            hp, max_hp = struct.unpack_from(
                "<HH",
                raw,
                human.state + base.STATE_HEALTH_OFFSET - memory_range[0],
            )
            health[human.human] = (hp, max_hp)
            if gang is not None and is_wandermoon(gang.name):
                action_targets[human.human] = struct.unpack_from(
                    "<I",
                    raw,
                    human.state + STATE_ACTION_TARGET_OFFSET - memory_range[0],
                )[0]

        wandermoons = [
            (human, gang)
            for human, gang in relevant
            if (
                gang is not None
                and is_wandermoon(gang.name)
                and 0 <= human.move_index < 0x100
            )
        ]
        brain_items = [
            (
                human,
                base.BRAIN_BASE + human.move_index * base.BRAIN_STRIDE,
            )
            for human, _gang in wandermoons
        ]
        brain_ranges = [
            base.aligned_range(address, BRAIN_DESTINATION_READ_SIZE)
            for _human, address in brain_items
        ]
        brain_rows = client.read_blocks(brain_ranges) if brain_ranges else []
        brains: dict[int, bytes] = {}
        for (human, address), memory_range, raw in zip(
            brain_items, brain_ranges, brain_rows, strict=True
        ):
            offset = address - memory_range[0]
            brains[human.human] = raw[
                offset : offset + BRAIN_DESTINATION_READ_SIZE
            ]

        goal_pointers = sorted({
            pointer
            for brain_raw in brains.values()
            for pointer in decode_goal_pointers(brain_raw)
        })
        goal_ranges = [
            base.aligned_range(pointer, GOAL_READ_SIZE)
            for pointer in goal_pointers
        ]
        goal_raw_rows = client.read_blocks(goal_ranges) if goal_ranges else []
        goal_rows: dict[int, bytes] = {}
        for pointer, memory_range, raw in zip(
            goal_pointers, goal_ranges, goal_raw_rows, strict=True
        ):
            offset = pointer - memory_range[0]
            goal_rows[pointer] = raw[offset : offset + GOAL_READ_SIZE]

        handle_names = {
            human.handle: human.name or f"Human 0x{human.handle:08X}"
            for human in humans
        }
        destinations = {
            human.human: resolve_wandermoon_destination(
                brains[human.human],
                goal_rows,
                action_targets.get(human.human, base.NIL_HANDLE),
                handle_names,
            )
            for human, _gang in wandermoons
            if human.human in brains
        }

        positioned = [item for item in relevant if item[0].human in health]
        transform_rows = client.read_blocks([
            (base.transform_address(human), 32) for human, _gang in positioned
        ]) if positioned else []
        actors: list[ActorReading] = []
        ungrouped_count = sum(gang is None for _human, gang in positioned)
        for (human, gang), transform_raw in zip(positioned, transform_rows, strict=True):
            hp, max_hp = health[human.human]
            if gang is None:
                name = NO_GANG
                label = NO_GANG
                color = gang_color(NO_GANG)
                remaining = ungrouped_count
            else:
                name = gang.name
                label = gang.label
                color = gang.color
                remaining = gang.remaining
            ai_goal, destination_kind, destination_name, destination_position = (
                destinations.get(
                    human.human,
                    (
                        "",
                        "unknown" if is_wandermoon(name) else "",
                        "",
                        None,
                    ),
                )
            )
            actors.append(
                ActorReading(
                    human=human.human,
                    handle=human.handle,
                    actor_name=human.name,
                    gang_name=name,
                    gang_label=label,
                    gang_color=color,
                    remaining=remaining,
                    hp=hp,
                    max_hp=max_hp,
                    position=base.decode_position(transform_raw),
                    ai_goal=ai_goal,
                    destination_kind=destination_kind,
                    destination_name=destination_name,
                    destination_position=destination_position,
                )
            )

        panel_gangs = [
            gang for gang in gangs_by_pointer.values()
            if not is_warriors(gang.name) and (gang.remaining or gang.spawners)
        ]
        if ungrouped_count:
            panel_gangs.append(
                GangReading(
                    pointer=0,
                    name=NO_GANG,
                    label=NO_GANG,
                    color=gang_color(NO_GANG),
                    remaining=ungrouped_count,
                    members=(),
                )
            )
        actors.sort(key=lambda actor: (actor.gang_label.casefold(), actor.handle))
        panel_gangs.sort(key=lambda gang: gang.label.casefold())
        current, maximum = self.read_global_counts(client)
        return WorldSnapshot(
            message="ONLINE · read only · native gangs and spawners",
            enemies=tuple(actors),
            gangs=tuple(panel_gangs),
            camera=camera,
            spawner_current=current,
            spawner_max=maximum,
        )


class GangWorker(threading.Thread):
    def __init__(self, *, port: int, sample_hz: float) -> None:
        super().__init__(name="AllGangWorker", daemon=True)
        self.port = port
        self.sample_hz = sample_hz
        self.reader = AllGangReader()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = WorldSnapshot()

    def snapshot(self) -> WorldSnapshot:
        with self.lock:
            return self.latest

    def publish(self, snapshot: WorldSnapshot) -> None:
        with self.lock:
            self.latest = snapshot

    def run_connection(self, client: PineClient) -> None:
        base.verify_game(client)
        interval = 1.0 / self.sample_hz
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                snapshot = self.reader.read(client)
            except (RuntimeError, ValueError, struct.error) as error:
                snapshot = replace(
                    self.snapshot(),
                    message=f"Resolving again after loading: {error}",
                    enemies=(),
                    gangs=(),
                    camera=None,
                    spawner_current=None,
                    spawner_max=None,
                )
            self.publish(snapshot)
            self.stop_event.wait(max(0.0, interval - (time.perf_counter() - started)))

    def run(self) -> None:
        while not self.stop_event.is_set():
            client: PineClient | None = None
            try:
                self.publish(replace(self.snapshot(), message="Connecting to PCSX2..."))
                client = PineClient(port=self.port, timeout=0.9)
                self.run_connection(client)
            except Exception as error:
                current = self.snapshot()
                self.publish(
                    WorldSnapshot(
                        message=f"Waiting for game: {error}",
                        reconnects=current.reconnects + 1,
                    )
                )
                self.stop_event.wait(0.5)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass

    def stop(self) -> None:
        self.stop_event.set()


class GangUdpWorker(threading.Thread):
    """Receive gang snapshots without opening a second PINE connection."""

    def __init__(self, *, state_port: int) -> None:
        super().__init__(name="AllGangUdpWorker", daemon=True)
        self.state_port = state_port
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = WorldSnapshot(message="Waiting for Freecam telemetry...")

    def snapshot(self) -> WorldSnapshot:
        with self.lock:
            return self.latest

    def publish(self, snapshot: WorldSnapshot) -> None:
        with self.lock:
            self.latest = snapshot

    def run(self) -> None:
        receiver: socket.socket | None = None
        try:
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            receiver.bind(("127.0.0.1", self.state_port))
            receiver.settimeout(0.25)
            last_packet = time.monotonic()
            stale_cleared = False
            while not self.stop_event.is_set():
                try:
                    packet, _address = receiver.recvfrom(65535)
                except TimeoutError:
                    if time.monotonic() - last_packet > 1.25 and not stale_cleared:
                        current = self.snapshot()
                        self.publish(
                            WorldSnapshot(
                                message="Waiting for Freecam telemetry...",
                                reconnects=current.reconnects + 1,
                            )
                        )
                        stale_cleared = True
                    continue
                try:
                    snapshot = snapshot_from_packet(packet)
                except (UnicodeError, ValueError, TypeError, KeyError, IndexError):
                    continue
                self.publish(snapshot)
                last_packet = time.monotonic()
                stale_cleared = False
        except OSError as error:
            self.publish(WorldSnapshot(message=f"Puerto UDP no disponible: {error}"))
        finally:
            if receiver is not None:
                receiver.close()

    def stop(self) -> None:
        self.stop_event.set()


class AllGangOverlay(base.GangOverlay):
    LABEL_WIDTH = 320
    LABEL_HEIGHT = 60

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.root.title("The Warriors - All Gang Overlay")

    def draw_enemy_label(self, projected: base.ProjectedEnemy) -> None:
        actor = projected.actor
        objective = destination_text(actor)
        x = projected.label_x
        y = projected.label_y
        half_width = self.LABEL_WIDTH * 0.5
        half_height = self.LABEL_HEIGHT * 0.5
        self.canvas.create_line(
            projected.anchor_x,
            projected.anchor_y,
            x,
            y + half_height,
            fill=actor.gang_color,
            width=1,
        )
        self.canvas.create_oval(
            projected.anchor_x - 2,
            projected.anchor_y - 2,
            projected.anchor_x + 2,
            projected.anchor_y + 2,
            fill=actor.gang_color,
            outline="",
        )
        self.canvas.create_rectangle(
            x - half_width,
            y - half_height,
            x + half_width,
            y + half_height,
            fill=LABEL_BG,
            outline=actor.gang_color,
            width=2,
        )
        self.canvas.create_text(
            x,
            y - 18 if objective else y - 9,
            text=f"{actor.gang_label} · {actor.remaining} members",
            fill=actor.gang_color,
            font=("Segoe UI", 9, "bold"),
        )
        self.canvas.create_text(
            x,
            y if objective else y + 9,
            text=f"{actor.actor_name} · HP {actor.hp}/{actor.max_hp}",
            fill=TEXT,
            font=("Segoe UI", 8, "bold"),
        )
        if objective:
            objective_color = {
                "attack": RED,
                "pursuit": YELLOW,
                "position": CYAN,
            }.get(actor.destination_kind, DIM)
            self.canvas.create_text(
                x,
                y + 18,
                text=objective,
                fill=objective_color,
                font=("Consolas", 8, "bold"),
            )

    @staticmethod
    def gang_entry_height(gang: GangReading) -> int:
        return max(38, 21 + len(gang.spawners) * 16)

    @classmethod
    def panel_columns(
        cls,
        gangs: tuple[GangReading, ...],
    ) -> tuple[tuple[GangReading, ...], ...]:
        if len(gangs) < 2:
            return (gangs, ())
        heights = [cls.gang_entry_height(gang) for gang in gangs]
        total = sum(heights)
        running = 0
        split = 1
        best_difference = math.inf
        for index, height in enumerate(heights[:-1], start=1):
            running += height
            difference = abs(total - 2 * running)
            if difference < best_difference:
                best_difference = difference
                split = index
        return (gangs[:split], gangs[split:])

    def draw_panel(self, snapshot: WorldSnapshot) -> None:
        left = self.x_offset
        top = self.y_offset
        column_width = 350
        width = column_width * 2
        columns = self.panel_columns(snapshot.gangs)
        content_heights = [
            sum(self.gang_entry_height(gang) for gang in column)
            for column in columns
        ]
        height = 90 + max(38, *content_heights)
        self.canvas.create_rectangle(
            left,
            top,
            left + width,
            top + height,
            fill=PANEL_BG,
            outline=PANEL_BORDER,
            width=2,
        )
        self.canvas.create_text(
            left + 12,
            top + 10,
            anchor="nw",
            text="ACTIVE GANGS · ALL HUMANS EXCEPT WARRIORS",
            fill=CYAN,
            font=("Consolas", 11, "bold"),
        )
        self.canvas.create_text(
            left + width - 12,
            top + 12,
            anchor="ne",
            text="Ctrl+Shift+Q exit",
            fill=DIM,
            font=("Consolas", 8),
        )
        if snapshot.spawner_current is None or snapshot.spawner_max is None:
            counter = "SPAWNER NPCs (motor): no disponible"
            counter_color = YELLOW
        else:
            free = snapshot.spawner_max - snapshot.spawner_current
            counter = (
                f"SPAWNER NPCs (motor): {snapshot.spawner_current}/"
                f"{snapshot.spawner_max} · margen {free}"
            )
            counter_color = RED if free <= 3 else GREEN
        self.canvas.create_text(
            left + 12,
            top + 34,
            anchor="nw",
            text=counter,
            fill=counter_color,
            font=("Consolas", 10, "bold"),
        )
        self.canvas.create_text(
            left + 12,
            top + 56,
            anchor="nw",
            text=(snapshot.message[:74] + " · *estimated camera gate"),
            fill=GREEN if snapshot.camera is not None else YELLOW,
            font=("Consolas", 8),
        )
        if not snapshot.gangs:
            self.canvas.create_text(
                left + 12,
                top + 82,
                anchor="nw",
                text="Waiting for NPCs/gangs in the current world...",
                fill=DIM,
                font=("Consolas", 9),
            )
            return
        for column_index, column in enumerate(columns):
            x = left + 12 + column_index * column_width
            y = top + 82
            for gang in column:
                self.canvas.create_text(
                    x,
                    y,
                    anchor="nw",
                    text=f"{gang.label}: {gang.remaining}",
                    fill=gang.color,
                    font=("Consolas", 9, "bold"),
                )
                for spawner_index, spawner in enumerate(gang.spawners):
                    self.canvas.create_text(
                        x,
                        y + 17 + spawner_index * 16,
                        anchor="nw",
                        text=spawner_gate_text(spawner)[:53],
                        fill=TEXT,
                        font=("Consolas", 7),
                    )
                y += self.gang_entry_height(gang)


def print_once(port: int) -> int:
    client = PineClient(port=port, timeout=1.0)
    try:
        base.verify_game(client)
        snapshot = AllGangReader().read(client)
    finally:
        client.close()
    print(snapshot.message)
    print(f"SPAWNER NPCs (motor): {snapshot.spawner_current}/{snapshot.spawner_max}")
    for gang in snapshot.gangs:
        print(f"{gang.name}: {gang.remaining} members")
        for spawner in gang.spawners:
            x, y, z = spawner.position
            print(
                f"  {spawner_gate_text(spawner)} | slot {spawner.slot + 1} "
                f"actual/objetivo={spawner.current}/{spawner.target} "
                f"pos=({x:.2f},{y:.2f},{z:.2f})"
            )
    for actor in snapshot.enemies:
        x, y, z = actor.position
        objective = destination_text(actor)
        print(
            f"0x{actor.handle:08X} {actor.actor_name:<16} | "
            f"{actor.gang_label:<16} | HP {actor.hp:3d}/{actor.max_hp:<3d} "
            f"pos=({x:.2f},{y:.2f},{z:.2f})"
            f"{' | ' + objective if objective else ''}"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Native gangs, NPCs and spawners overlay"
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument(
        "--state-port",
        type=int,
        default=0,
        help="receive gang snapshots over UDP instead of opening PINE",
    )
    parser.add_argument("--sample-hz", type=float, default=10.0)
    parser.add_argument("--x-offset", type=int, default=18)
    parser.add_argument("--y-offset", type=int, default=18)
    parser.add_argument("--opacity", type=float, default=0.72)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 2.0 <= args.sample_hz <= 30.0:
        raise ValueError("--sample-hz must be between 2 and 30")
    if not 0.25 <= args.opacity <= 1.0:
        raise ValueError("--opacity must be between 0.25 and 1.0")
    if args.once and args.state_port:
        raise ValueError("--once cannot be combined with --state-port")

    instance_handle = acquire_single_instance()
    if not instance_handle:
        if args.once:
            print("The generic overlay is already running")
            raise SystemExit(3)
        return
    try:
        if args.once:
            raise SystemExit(print_once(args.port))
        worker = (
            GangUdpWorker(state_port=args.state_port)
            if args.state_port
            else GangWorker(port=args.port, sample_hz=args.sample_hz)
        )
        worker.start()
        root = tk.Tk()
        overlay = AllGangOverlay(
            root,
            worker=worker,
            x_offset=args.x_offset,
            y_offset=args.y_offset,
            opacity=args.opacity,
        )
        try:
            overlay.run()
        finally:
            if not overlay.closed:
                overlay.close()
    finally:
        release_single_instance(instance_handle)


if __name__ == "__main__":
    main()
