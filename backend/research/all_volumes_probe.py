#!/usr/bin/env python3
"""Render multiple extracted All-City volumes in the confirmed 3D camera pass.

The game's heap currently permits two reliable 0x1000-byte allocations but
rejects a third.  This probe stores every box as eight corners plus one color,
then expands each compact record into one reusable RwIm3D vertex buffer from a
single reversible hook at 0x00185BFC.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
import time
from pathlib import Path

from freecam_probe import resolve_camera_transform
from ingame_volume_probe import (
    BLOB_SIZE,
    BOOTSTRAP_ADDRESS,
    BOOTSTRAP_SIZE,
    KIND_COLORS,
    MARKER,
    MARKER_OFFSET,
    REG,
    RW_IM3D_END,
    RW_IM3D_RENDER_PRIMITIVE,
    RW_IM3D_TRANSFORM,
    RW_MATRIX_SET_IDENTITY,
    addiu,
    beq,
    bne,
    build_vertices,
    call_render_state,
    jal,
    jr,
    load_address,
    lw,
    run_heap_bootstrap,
    sw,
)
from ingame_wrapper_probe import (
    HOOK_ADDRESS,
    HOOK_ORIGINAL,
    INNER_VIEW_HOOK_ADDRESS,
    INNER_VIEW_HOOK_ORIGINAL,
    MAIN_VIEW_HOOK_ADDRESS,
    MAIN_VIEW_HOOK_ORIGINAL,
    MAIN_VIEW_ORIGINAL_CALLEE,
    convert_vertices_to_render_space,
)
from pine_client import GAME_ID, TITLE, PineClient


TRACE_BEGIN_HOOK = 0x00198948
TRACE_BEGIN_ORIGINAL = 0x0C1209EE
TRACE_END_HOOK = 0x00198968
TRACE_END_ORIGINAL = 0x0C1209F6
DANGEROUS_END_SCENE_HOOK = 0x00159418
DANGEROUS_END_SCENE_ORIGINAL = 0x0060F809

STATUS_OFFSET = 0xFC0
MATRIX_OFFSET = 0xF80
DESTINATION_OFFSET = 0x800
PLAYER_POSITION_OFFSET = 0x3E0
PROJECTION_OUTPUT_OFFSET = 0x400
VERTICES_PER_SOLID_BOX = 36
COMPACT_RECORD_SIZE = 116
KIND_SLOT_COUNT = 10
DATA_MARKER = b"TW_MULTI_DATA_V1"
COLLISION_DATA_MARKER = b"TW_COLLISION_V1\0"
WORLD_TO_SCREEN = 0x00198460
GET_PLAYER = 0x00234FB8
GET_HUMAN_POSITION = 0x002352B0
T2 = 10
T3 = 11
T4 = 12
T5 = 13
S3 = 19
S4 = 20


def i_type(opcode: int, rs: int, rt: int, immediate: int) -> int:
    return (opcode << 26) | (rs << 21) | (rt << 16) | (immediate & 0xFFFF)


def r_type(rs: int, rt: int, rd: int, shift: int, function: int) -> int:
    return (rs << 21) | (rt << 16) | (rd << 11) | (shift << 6) | function


def lbu(rt: int, offset: int, base: int) -> int:
    return i_type(0x24, base, rt, offset)


def sll(rd: int, rt: int, shift: int) -> int:
    return r_type(0, rt, rd, shift, 0x00)


def addu(rd: int, rs: int, rt: int) -> int:
    return r_type(rs, rt, rd, 0, 0x21)


def subu(rd: int, rs: int, rt: int) -> int:
    return r_type(rs, rt, rd, 0, 0x23)


def volume_center(volume: dict) -> tuple[float, float, float]:
    return tuple(
        volume["origin"][index] + volume["size"][index] * 0.5
        for index in range(3)
    )


def select_volumes(
    document: dict,
    camera_position: tuple[float, float, float],
    max_distance: float,
    limit: int,
) -> list[tuple[float, dict]]:
    rows = [
        (math.dist(camera_position, volume_center(volume)), volume)
        for volume in document["volumes"]
    ]
    rows.sort(key=lambda item: item[0])
    if max_distance > 0:
        rows = [item for item in rows if item[0] <= max_distance]
    if limit > 0:
        rows = rows[:limit]
    return rows


def compact_box(volume: dict) -> tuple[bytes, bytes]:
    """Return eight RW corners, color, center, and shared triangle indices."""
    vertices = bytearray(build_vertices(volume, solid=True))
    convert_vertices_to_render_space(vertices)
    corners: list[bytes] = []
    corner_lookup: dict[bytes, int] = {}
    indices = bytearray()
    for offset in range(0, len(vertices), 36):
        position = bytes(vertices[offset : offset + 12])
        index = corner_lookup.get(position)
        if index is None:
            index = len(corners)
            corner_lookup[position] = index
            corners.append(position)
        indices.append(index)
    if len(corners) != 8 or len(indices) != VERTICES_PER_SOLID_BOX:
        raise ValueError(f"unexpected solid-box topology for {volume['name']}")
    color = bytes(vertices[12:16])
    center_x, center_y, center_z = volume_center(volume)
    render_center = struct.pack("<4f", center_x, center_z, -center_y, 1.0)
    return b"".join(corners) + color + render_center, bytes(indices)


def build_compact_data(volumes: list[dict]) -> tuple[bytes, int, int, int]:
    records = bytearray()
    shared_indices: bytes | None = None
    for volume in volumes:
        record, indices = compact_box(volume)
        if len(record) != COMPACT_RECORD_SIZE:
            raise ValueError("compact box record has an unexpected size")
        if shared_indices is None:
            shared_indices = indices
        elif indices != shared_indices:
            raise ValueError("solid boxes do not share one triangle topology")
        records.extend(record)
    if shared_indices is None:
        raise ValueError("at least one volume is required")
    index_offset = (len(records) + 3) & ~3
    kind_offset = index_offset + len(shared_indices)
    visibility_offset = kind_offset + len(volumes)
    if visibility_offset + KIND_SLOT_COUNT > MARKER_OFFSET:
        raise ValueError("compact volume data exceed one heap block")
    blob = bytearray(BLOB_SIZE)
    blob[: len(records)] = records
    blob[index_offset : index_offset + len(shared_indices)] = shared_indices
    for index, volume in enumerate(volumes):
        kind = int(volume["kind"])
        if kind not in range(KIND_SLOT_COUNT):
            raise ValueError(
                f"volume kind {kind} is outside the keyboard-filterable range 0-9"
            )
        blob[kind_offset + index] = kind
    blob[
        visibility_offset : visibility_offset + KIND_SLOT_COUNT
    ] = bytes([1]) * KIND_SLOT_COUNT
    blob[MARKER_OFFSET : MARKER_OFFSET + len(DATA_MARKER)] = DATA_MARKER
    return bytes(blob), index_offset, kind_offset, visibility_offset


def build_render_code(
    code_address: int,
    data_address: int,
    index_address: int,
    kind_address: int,
    visibility_address: int,
    volume_count: int,
) -> bytes:
    status_address = code_address + STATUS_OFFSET
    matrix_address = code_address + MATRIX_OFFSET
    destination_address = code_address + DESTINATION_OFFSET
    words: list[int] = [
        addiu(REG["sp"], REG["sp"], -0x60),
        sw(REG["ra"], 0x50, REG["sp"]),
        sw(REG["s0"], 0x40, REG["sp"]),
        sw(REG["s1"], 0x44, REG["sp"]),
        sw(REG["s2"], 0x48, REG["sp"]),
        sw(S3, 0x4C, REG["sp"]),
        sw(S4, 0x38, REG["sp"]),
    ]

    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 0, REG["t0"]))

    # Preserve the displaced call and its result, consumed at 0x00185C0C.
    words.append(jal(MAIN_VIEW_ORIGINAL_CALLEE))
    words.append(0)
    words.append(sw(REG["v0"], 0x34, REG["sp"]))

    # Resolve player zero through the same two helpers used by the registered
    # Lua functions HuGetPlayer and HuGetPosition, then expose xyz to the
    # companion overlay.  This deliberately follows the character, never the
    # logical/free camera.
    words.append(addiu(REG["a0"], REG["zero"], 0))
    words.append(jal(GET_PLAYER))
    words.append(0)
    words.append(addiu(REG["a0"], REG["v0"], 0))
    words.append(jal(GET_HUMAN_POSITION))
    words.append(0)
    load_address(words, T2, code_address + PLAYER_POSITION_OFFSET)
    words.extend(
        (
            lw(T3, 0, REG["v0"]),
            sw(T3, 0, T2),
            lw(T3, 4, REG["v0"]),
            sw(T3, 4, T2),
            lw(T3, 8, REG["v0"]),
            sw(T3, 8, T2),
            lw(T3, 12, REG["v0"]),
            sw(T3, 12, T2),
        )
    )

    # This is the exact normal-depth setup validated by the one-volume probe.
    call_render_state(words, 0x10, 7, value=1)
    call_render_state(words, 0x10, 1, value=0)
    call_render_state(words, 0x10, 14, value=0)
    load_address(words, REG["a0"], matrix_address)
    words.append(jal(RW_MATRIX_SET_IDENTITY))
    words.append(0)

    # Keep the compact source and loop state in callee-saved registers.  Each
    # box is expanded into the same reusable 36-vertex RwIm3D buffer.
    load_address(words, REG["s0"], data_address)
    words.append(addiu(REG["s1"], REG["zero"], volume_count))
    load_address(words, S3, index_address)
    load_address(words, S4, code_address + PROJECTION_OUTPUT_OFFSET)
    outer_loop = len(words)

    # Project this box's exact RenderWare-space midpoint through the currently
    # open main view.  The caller's original s3 is the DevRWView object and is
    # already saved at sp+0x4C before s3 is reused for the triangle indices.
    # The external label layer reads these 16-byte results in one PINE block.
    words.extend(
        (
            lw(T2, 100, REG["s0"]),
            sw(T2, 0, S4),
            lw(T2, 104, REG["s0"]),
            sw(T2, 4, S4),
            lw(T2, 108, REG["s0"]),
            sw(T2, 8, S4),
            lw(T2, 112, REG["s0"]),
            sw(T2, 12, S4),
            addiu(REG["a0"], S4, 0),
            lw(REG["a1"], 0x4C, REG["sp"]),
            jal(WORLD_TO_SCREEN),
            0,
            # Replace the no-longer-needed homogeneous w with the camera's
            # projected-depth bias.  A point is in front when z > bias.
            lw(T2, 0x4C, REG["sp"]),
            lw(T2, 4, T2),
            lw(T3, 0x90, T2),
            sw(T3, 12, S4),
            addiu(S4, S4, 16),
        )
    )

    # Use the current record index to find its kind, then skip the whole box
    # when the host-controlled 0-9 visibility slot is disabled.
    load_address(words, T2, kind_address)
    words.append(addiu(T3, REG["zero"], volume_count))
    words.append(subu(T3, T3, REG["s1"]))
    words.append(addu(T2, T2, T3))
    words.append(lbu(T5, 0, T2))
    load_address(words, T2, visibility_address)
    words.append(addu(T2, T2, T5))
    words.append(lbu(T2, 0, T2))
    hidden = len(words)
    words.extend((0, 0))

    load_address(words, REG["s2"], destination_address)
    words.append(addiu(REG["t0"], S3, 0))
    words.append(addiu(REG["t1"], REG["zero"], VERTICES_PER_SOLID_BOX))
    words.append(lw(T5, 96, REG["s0"]))
    inner_loop = len(words)
    words.extend(
        (
            lbu(T2, 0, REG["t0"]),
            sll(T3, T2, 3),
            sll(T4, T2, 2),
            addu(T3, T3, T4),
            addu(T3, REG["s0"], T3),
            lw(T4, 0, T3),
            sw(T4, 0, REG["s2"]),
            lw(T4, 4, T3),
            sw(T4, 4, REG["s2"]),
            lw(T4, 8, T3),
            sw(T4, 8, REG["s2"]),
            sw(T5, 12, REG["s2"]),
            addiu(REG["t0"], REG["t0"], 1),
            addiu(REG["s2"], REG["s2"], 36),
            addiu(REG["t1"], REG["t1"], -1),
        )
    )
    inner_branch = len(words)
    words.append(
        bne(
            REG["t1"],
            REG["zero"],
            inner_loop - (inner_branch + 1),
        )
    )
    words.append(0)

    load_address(words, REG["a0"], destination_address)
    words.append(
        addiu(REG["a1"], REG["zero"], VERTICES_PER_SOLID_BOX)
    )
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))
    failed = len(words)
    words.append(0)
    words.append(0)

    words.append(addiu(REG["a0"], REG["zero"], 3))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 8, REG["t0"]))
    words.append(jal(RW_IM3D_END))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 12, REG["t0"]))
    words.append(lw(REG["t1"], 16, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 16, REG["t0"]))
    box_end = len(words)
    words[failed] = beq(
        REG["v0"], REG["zero"], box_end - (failed + 1)
    )
    words[hidden] = beq(
        T2, REG["zero"], box_end - (hidden + 1)
    )
    words.append(addiu(REG["s0"], REG["s0"], COMPACT_RECORD_SIZE))
    words.append(addiu(REG["s1"], REG["s1"], -1))
    outer_branch = len(words)
    words.append(
        bne(
            REG["s1"],
            REG["zero"],
            outer_loop - (outer_branch + 1),
        )
    )
    words.append(0)

    call_render_state(words, 0x10, 7, value=2)
    call_render_state(words, 0x10, 1, value=0)
    words.extend(
        (
            lw(REG["v0"], 0x34, REG["sp"]),
            lw(S4, 0x38, REG["sp"]),
            lw(S3, 0x4C, REG["sp"]),
            lw(REG["s2"], 0x48, REG["sp"]),
            lw(REG["s1"], 0x44, REG["sp"]),
            lw(REG["s0"], 0x40, REG["sp"]),
            lw(REG["ra"], 0x50, REG["sp"]),
            addiu(REG["sp"], REG["sp"], 0x60),
            jr(REG["ra"]),
            0,
        )
    )
    return struct.pack(f"<{len(words)}I", *words)


def build_code_block(
    code_address: int,
    data_address: int,
    index_address: int,
    kind_address: int,
    visibility_address: int,
    volume_count: int,
) -> tuple[bytes, int]:
    code = build_render_code(
        code_address,
        data_address,
        index_address,
        kind_address,
        visibility_address,
        volume_count,
    )
    if len(code) > PLAYER_POSITION_OFFSET:
        raise ValueError(f"multi-volume render code is too large: 0x{len(code):X}")
    projection_end = PROJECTION_OUTPUT_OFFSET + volume_count * 16
    if projection_end > DESTINATION_OFFSET:
        raise ValueError("projected label positions overlap the vertex buffer")
    destination_end = (
        DESTINATION_OFFSET + VERTICES_PER_SOLID_BOX * 36
    )
    if destination_end > STATUS_OFFSET:
        raise ValueError("reusable RwIm3D buffer overlaps status data")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    struct.pack_into("<I", blob, STATUS_OFFSET + 20, volume_count)
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(blob), len(code)


def validate_clean_state(client: PineClient) -> None:
    checks = (
        (HOOK_ADDRESS, HOOK_ORIGINAL, "stable hook"),
        (MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, "main-view hook"),
        (INNER_VIEW_HOOK_ADDRESS, INNER_VIEW_HOOK_ORIGINAL, "HUD-view hook"),
        (TRACE_BEGIN_HOOK, TRACE_BEGIN_ORIGINAL, "BeginUpdate trace hook"),
        (TRACE_END_HOOK, TRACE_END_ORIGINAL, "EndUpdate trace hook"),
        (
            DANGEROUS_END_SCENE_HOOK,
            DANGEROUS_END_SCENE_ORIGINAL,
            "EndScene call site",
        ),
    )
    for address, expected, label in checks:
        current = client.read(address, 32)
        if current != expected:
            raise RuntimeError(
                f"{label} mismatch at 0x{address:08X}: 0x{current:08X}"
            )
    gap = client.read_block(BOOTSTRAP_ADDRESS, BOOTSTRAP_SIZE, progress=False)
    if any(gap):
        raise RuntimeError("ELF bootstrap alignment gap is unexpectedly occupied")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "volumes",
        type=Path,
        nargs="?",
        default=Path("research/all_city_volumes.json"),
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--max-distance", type=float, default=0.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--freecam", action="store_true")
    parser.add_argument("--freecam-speed", type=float, default=8.0)
    parser.add_argument("--mouse-sensitivity", type=float, default=0.14)
    parser.add_argument(
        "--install-only",
        action="store_true",
        help="install the renderer, print both heap pointers, and leave it active",
    )
    parser.add_argument(
        "--cleanup-code-pointer",
        type=lambda value: int(value, 0),
        help="code pointer printed by --install-only",
    )
    parser.add_argument(
        "--cleanup-data-pointer",
        type=lambda value: int(value, 0),
        help="data pointer printed by --install-only",
    )
    args = parser.parse_args()
    if args.duration <= 0:
        raise ValueError("duration must be positive")
    if args.max_distance < 0 or args.limit < 0:
        raise ValueError("max-distance and limit cannot be negative")
    cleanup_requested = (
        args.cleanup_code_pointer is not None
        or args.cleanup_data_pointer is not None
    )
    if cleanup_requested and (
        args.cleanup_code_pointer is None
        or args.cleanup_data_pointer is None
    ):
        raise ValueError("cleanup requires both code and data pointers")
    if args.install_only and cleanup_requested:
        raise ValueError("install-only and cleanup are mutually exclusive")

    document = json.loads(args.volumes.read_text(encoding="utf-8"))
    client: PineClient | None = PineClient(port=args.port)
    allocations: list[int] = []
    hook_installed = False
    try:
        assert client is not None
        if client.text(GAME_ID) != "SLUS-21215":
            raise RuntimeError("The Warriors SLUS-21215 is not running")
        if cleanup_requested:
            assert args.cleanup_code_pointer is not None
            assert args.cleanup_data_pointer is not None
            code_marker = client.read_block(
                args.cleanup_code_pointer + MARKER_OFFSET,
                16,
                progress=False,
            )
            data_marker = client.read_block(
                args.cleanup_data_pointer + MARKER_OFFSET,
                16,
                progress=False,
            )
            if code_marker != MARKER:
                raise RuntimeError("multi-volume code marker mismatch")
            if data_marker not in (DATA_MARKER, COLLISION_DATA_MARKER):
                raise RuntimeError("multi-volume data marker mismatch")
            current_main_hook = client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
            installed_main_hook = jal(args.cleanup_code_pointer)
            if current_main_hook not in (
                MAIN_VIEW_HOOK_ORIGINAL,
                installed_main_hook,
            ):
                raise RuntimeError(
                    f"unexpected main-view hook: 0x{current_main_hook:08X}"
                )
            client.write(
                MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32
            )
            if (
                client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
                != MAIN_VIEW_HOOK_ORIGINAL
            ):
                raise RuntimeError("main-view hook restoration did not verify")
            run_heap_bootstrap(
                client, args.cleanup_data_pointer, freeing=True
            )
            run_heap_bootstrap(
                client, args.cleanup_code_pointer, freeing=True
            )
            print(
                f"cleaned multi-volume renderer code="
                f"0x{args.cleanup_code_pointer:08X} data="
                f"0x{args.cleanup_data_pointer:08X}"
            )
            return

        validate_clean_state(client)
        transform = resolve_camera_transform(client)
        raw_position = client.read_block(transform, 16, progress=False)
        camera_position = struct.unpack_from("<3f", raw_position)
        selected_rows = select_volumes(
            document, camera_position, args.max_distance, args.limit
        )
        if not selected_rows:
            raise RuntimeError("no volumes matched the requested selection")
        selected = [volume for _distance, volume in selected_rows]
        (
            data_blob,
            index_offset,
            kind_offset,
            visibility_offset,
        ) = build_compact_data(selected)
        _preview, code_size = build_code_block(
            0x00800000,
            0x00900000,
            0x00900000 + index_offset,
            0x00900000 + kind_offset,
            0x00900000 + visibility_offset,
            len(selected),
        )
        print(
            f"game={client.text(TITLE)} selected={len(selected)} "
            f"blocks=2 code=0x{code_size:X} data=0x{index_offset + 36:X}"
        )
        selected_kind_counts: dict[int, int] = {}
        for volume in selected:
            kind = volume["kind"]
            selected_kind_counts[kind] = selected_kind_counts.get(kind, 0) + 1
        for kind in sorted(selected_kind_counts):
            red, green, blue, _alpha = KIND_COLORS.get(
                kind, (255, 255, 255, 255)
            )
            print(
                f"  kind={kind} count={selected_kind_counts[kind]} "
                f"rgb=({red},{green},{blue})"
            )
        for distance, volume in selected_rows:
            print(f"  {distance:7.2f}m {volume['name']}")
        if args.dry_run:
            print("dry-run: validated compact layout; no memory was changed")
            return

        code_address = run_heap_bootstrap(client, BLOB_SIZE)
        allocations.append(code_address)
        data_address = run_heap_bootstrap(client, BLOB_SIZE)
        if data_address in allocations:
            raise RuntimeError(f"duplicate EE heap pointer: 0x{data_address:08X}")
        allocations.append(data_address)
        if not all(0x00700000 <= address < 0x01FF0000 for address in allocations):
            raise RuntimeError("one or more EE heap pointers are outside the expected range")

        client.write_block(data_address, data_blob)
        code_blob, _ = build_code_block(
            code_address,
            data_address,
            data_address + index_offset,
            data_address + kind_offset,
            data_address + visibility_offset,
            len(selected),
        )
        client.write_block(code_address, code_blob)
        client.write(MAIN_VIEW_HOOK_ADDRESS, jal(code_address), 32)
        hook_installed = True
        print(
            f"multi-volume render active code=0x{code_address:08X} "
            f"blocks={len(allocations)}",
            flush=True,
        )

        if args.install_only:
            print(f"ACTIVE_CODE_POINTER=0x{code_address:08X}", flush=True)
            print(f"ACTIVE_DATA_POINTER=0x{data_address:08X}", flush=True)
            print(
                f"ACTIVE_KIND_VISIBILITY_POINTER="
                f"0x{data_address + visibility_offset:08X}",
                flush=True,
            )
            print(
                f"ACTIVE_LABEL_POINTER="
                f"0x{code_address + PROJECTION_OUTPUT_OFFSET:08X}",
                flush=True,
            )
            print(f"ACTIVE_LABEL_COUNT={len(selected)}", flush=True)
            print(
                f"ACTIVE_PLAYER_POSITION_POINTER="
                f"0x{code_address + PLAYER_POSITION_OFFSET:08X}",
                flush=True,
            )
            # Ownership transfers to the explicit two-pointer cleanup mode.
            hook_installed = False
            allocations.clear()
            return

        if args.freecam:
            client.close()
            client = None
            freecam_script = Path(__file__).with_name("freecam_probe.py")
            subprocess.run(
                [
                    sys.executable,
                    str(freecam_script),
                    "--duration",
                    str(args.duration),
                    "--speed",
                    str(args.freecam_speed),
                    "--mouse-sensitivity",
                    str(args.mouse_sensitivity),
                    "--port",
                    str(args.port),
                    "--kind-visibility-address",
                    str(data_address + visibility_offset),
                ],
                check=True,
            )
            client = PineClient(port=args.port)
        else:
            time.sleep(args.duration)

        assert client is not None
        client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
        hook_installed = False
        status = struct.unpack(
            "<6I",
            client.read_block(
                code_address + STATUS_OFFSET, 24, progress=False
            ),
        )
        print(
            f"render-status executions={status[0]} "
            f"transform=0x{status[1]:08X} primitive={status[2]} "
            f"end={status[3]} volume_draws={status[4]} volumes={status[5]}"
        )
    except KeyboardInterrupt:
        pass
    finally:
        if client is None:
            try:
                client = PineClient(port=args.port)
            except Exception as error:
                print(f"WARNING: could not reconnect for cleanup: {error}")
        cleanup_safe = client is not None
        if hook_installed and client is not None:
            try:
                client.write(
                    MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32
                )
                hook_installed = False
                print("restored main-view hook")
            except Exception as error:
                cleanup_safe = False
                print(f"WARNING: could not restore main-view hook: {error}")
        if cleanup_safe and client is not None and allocations:
            try:
                cleanup_safe = (
                    client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
                    == MAIN_VIEW_HOOK_ORIGINAL
                )
            except Exception as error:
                cleanup_safe = False
                print(f"WARNING: could not verify main-view hook: {error}")
        if cleanup_safe and client is not None:
            for address in reversed(allocations):
                try:
                    run_heap_bootstrap(client, address, freeing=True)
                    print(f"released EE heap block 0x{address:08X}")
                except Exception as error:
                    print(
                        f"WARNING: could not release 0x{address:08X}: {error}"
                    )
        elif allocations:
            print("WARNING: retained heap blocks because hook cleanup was not verified")
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
