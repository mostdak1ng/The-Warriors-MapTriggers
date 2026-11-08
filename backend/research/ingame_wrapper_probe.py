#!/usr/bin/env python3
"""Safe in-game box probe through The Warriors' own Im3D wrapper.

This hooks the stable call to the game's queued-effect flush at 0x001567C4,
lets that flush run normally, and then submits one world-space box through the
same graphics wrapper used by the game.  The hook is restored before the heap
block is released.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import struct
import sys
import time
from pathlib import Path

from ingame_volume_probe import (
    BLOB_SIZE,
    GAME_FREE,
    MARKER,
    MARKER_OFFSET,
    RENDER_STATES,
    RW_IM3D_END,
    RW_IM3D_RENDER_PRIMITIVE,
    RW_IM3D_TRANSFORM,
    RW_MATRIX_SET_IDENTITY,
    REG,
    addiu,
    beq,
    bne,
    build_vertices,
    call_render_state,
    find_volume,
    jal,
    jr,
    load_address,
    lw,
    run_heap_bootstrap,
    sw,
)
from freecam_probe import normalize_quaternion, resolve_camera_transform, rotate
from pine_client import GAME_ID, TITLE, PineClient


HOOK_ADDRESS = 0x001567C4
HOOK_ORIGINAL = 0x0C05F0C2  # jal 0x0017C308
ORIGINAL_CALLEE = 0x0017C308
ACTIVE_PASS_HOOK_ADDRESS = 0x0015671C
ACTIVE_PASS_HOOK_ORIGINAL = 0x0C061748  # jal 0x00185D20
ACTIVE_PASS_ORIGINAL_CALLEE = 0x00185D20
INNER_VIEW_HOOK_ADDRESS = 0x00185DEC
INNER_VIEW_HOOK_ORIGINAL = 0x0C11C754  # jal 0x00471D50
INNER_VIEW_ORIGINAL_CALLEE = 0x00471D50
MAIN_VIEW_HOOK_ADDRESS = 0x00185BFC
MAIN_VIEW_HOOK_ORIGINAL = 0x0C11C754  # jal 0x00471D50
MAIN_VIEW_ORIGINAL_CALLEE = 0x00471D50

# DevRWGraphicsDevice owns an immediate-mode helper at +4.  Its world-space
# vertex buffer/count are consumed by GraphicsImmediate::Flush at 0x196B58.
GRAPHICS_DEVICE_GLOBAL = 0x0050CDB4
GAME_IM3D_VERTEX_BUFFER = 0x0050CE94
GAME_IM3D_VERTEX_COUNT = 0x0050CE88
GAME_IM2D_BEGIN = 0x001964C0
GAME_IM2D_APPEND = 0x00196548
GAME_IM2D_FLUSH = 0x001965D8
GAME_IM3D_BEGIN = 0x00196730
GAME_IM3D_APPEND = 0x00196748
GAME_IM3D_FLUSH = 0x00196B58
GAME_CAMERA_COUNT = 0x0050B198
GAME_VIEW_BEGIN_UPDATE = 0x00198940
GAME_VIEW_END_UPDATE = 0x00198960

STATUS_OFFSET = 0x7D0
SOURCE_VERTEX_OFFSET = 0x200
VERTEX_OFFSET = 0x800
SCREEN_SOURCE_OFFSET = 0x600
SCREEN_DUMMY_OFFSET = 0x6D0


def build_camera_test_volume(
    client: PineClient, distance: float, size: float
) -> dict:
    """Build a large axis-aligned box directly in front of the live camera."""
    camera_transform = resolve_camera_transform(client)
    raw = client.read_block(camera_transform, 32, progress=False)
    values = struct.unpack("<8f", raw)
    position = values[:3]
    quaternion = normalize_quaternion(values[4:8])
    forward = rotate(quaternion, (0.0, 1.0, 0.0))
    center = [
        position[index] + forward[index] * distance for index in range(3)
    ]
    half_size = size * 0.5
    volume = {
        "name": "CameraTestBox",
        "kind": 0,
        "origin": [component - half_size for component in center],
        "size": [size, size, size],
        "rotation_xy": None,
    }
    print(
        f"camera-test transform=0x{camera_transform:08X} "
        f"position=({position[0]:.3f}, {position[1]:.3f}, {position[2]:.3f}) "
        f"center=({center[0]:.3f}, {center[1]:.3f}, {center[2]:.3f}) "
        f"size={size:.3f}"
    )
    return volume


def game_to_render_position(
    position: tuple[float, float, float] | list[float],
) -> tuple[float, float, float]:
    """Convert gameplay/Lua XYZ into RenderWare world XYZ."""
    x, y, z = position
    return x, z, -y


def convert_vertices_to_render_space(vertices: bytearray) -> None:
    for offset in range(0, len(vertices), 36):
        game_position = struct.unpack_from("<3f", vertices, offset)
        struct.pack_into(
            "<3f", vertices, offset, *game_to_render_position(game_position)
        )


def build_code(
    source_vertex_address: int,
    vertex_address: int,
    vertex_count: int,
    matrix_address: int,
    status_address: int,
    primitive_type: int,
    wrapper_builder: bool,
    force_visible: bool,
    diagnose_view: bool,
    original_callee: int = ORIGINAL_CALLEE,
) -> bytes:
    words: list[int] = [
        addiu(REG["sp"], REG["sp"], -0x50),
        sw(REG["ra"], 0x40, REG["sp"]),
        sw(REG["s0"], 0x30, REG["sp"]),
        sw(REG["s1"], 0x34, REG["sp"]),
        sw(REG["s2"], 0x38, REG["sp"]),
    ]

    # Count executions, then preserve the game's normal effect flush.
    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 0, REG["t0"]))
    words.append(jal(original_callee))
    words.append(0)

    # Record the live number of registered views at the exact hook moment.
    load_address(words, REG["t0"], GAME_CAMERA_COUNT)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["t1"], 8, REG["t0"]))

    if diagnose_view:
        # Mirror the first view's balanced BeginUpdate/EndUpdate sequence and
        # record whether the view can be opened at this exact hook point.
        load_address(words, REG["t0"], GRAPHICS_DEVICE_GLOBAL)
        words.append(lw(REG["a0"], 0, REG["t0"]))
        words.append(addiu(REG["a0"], REG["a0"], 0x70))
        words.append(jal(GAME_VIEW_BEGIN_UPDATE))
        words.append(0)
        load_address(words, REG["t0"], status_address)
        words.append(sw(REG["v0"], 12, REG["t0"]))
        branch_index = len(words)
        words.append(0)  # beq v0, zero, skip EndUpdate
        words.append(0)
        load_address(words, REG["t0"], GRAPHICS_DEVICE_GLOBAL)
        words.append(lw(REG["a0"], 0, REG["t0"]))
        words.append(addiu(REG["a0"], REG["a0"], 0x70))
        words.append(jal(GAME_VIEW_END_UPDATE))
        words.append(0)
        skip_end_index = len(words)
        words[branch_index] = beq(
            REG["v0"], REG["zero"], skip_end_index - (branch_index + 1)
        )

    # Feed the game's own world-space Im3D buffer.  The original queue has
    # just been drained, so these globals are scratch at this point.
    if wrapper_builder:
        # Resolve DevRWGraphicsDevice::immediate (+4), initialize a separate
        # destination buffer through Begin, then append each 36-byte vertex
        # through the helper itself.
        load_address(words, REG["t0"], GRAPHICS_DEVICE_GLOBAL)
        words.append(lw(REG["s2"], 0, REG["t0"]))
        words.append(lw(REG["s2"], 4, REG["s2"]))
        words.append(addiu(REG["a0"], REG["s2"], 0))
        load_address(words, REG["a1"], vertex_address)
        words.append(jal(GAME_IM3D_BEGIN))
        words.append(0)

        load_address(words, REG["s0"], source_vertex_address)
        words.append(addiu(REG["s1"], REG["zero"], vertex_count))
        loop_index = len(words)
        words.append(addiu(REG["a0"], REG["s2"], 0))
        words.append(addiu(REG["a1"], REG["s0"], 0))
        words.append(addiu(REG["a2"], REG["s0"], 0x0C))
        words.append(addiu(REG["a3"], REG["s0"], 0x1C))
        words.append(jal(GAME_IM3D_APPEND))
        words.append(0)
        words.append(addiu(REG["s0"], REG["s0"], 36))
        words.append(addiu(REG["s1"], REG["s1"], -1))
        branch_index = len(words)
        words.append(
            bne(
                REG["s1"],
                REG["zero"],
                loop_index - (branch_index + 1),
            )
        )
        words.append(0)
    else:
        load_address(words, REG["t0"], GAME_IM3D_VERTEX_BUFFER)
        load_address(words, REG["t1"], vertex_address)
        words.append(sw(REG["t1"], 0, REG["t0"]))
        load_address(words, REG["t0"], GAME_IM3D_VERTEX_COUNT)
        words.append(addiu(REG["t1"], REG["zero"], vertex_count))
        words.append(sw(REG["t1"], 0, REG["t0"]))

    load_address(words, REG["a0"], matrix_address)
    words.append(jal(RW_MATRIX_SET_IDENTITY))
    words.append(0)

    if force_visible:
        # Calibration-only state: no depth rejection/writes and no face
        # culling.  Restore the normal game defaults immediately afterward.
        call_render_state(words, 0x10, 6, value=0)
        call_render_state(words, 0x10, 8, value=0)
        call_render_state(words, 0x10, 20, value=1)

    # a0 is unused by this concrete implementation; a1 is the optional
    # texture raster, a2 the world matrix, and a3 the RwPrimitiveType.
    words.append(addiu(REG["a0"], REG["zero"], 0))
    words.append(addiu(REG["a1"], REG["zero"], 0))
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], primitive_type))
    words.append(jal(GAME_IM3D_FLUSH))
    words.append(0)

    if force_visible:
        call_render_state(words, 0x10, 20, value=2)
        call_render_state(words, 0x10, 8, value=1)
        call_render_state(words, 0x10, 6, value=1)

    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))
    words.extend(
        (
            lw(REG["s2"], 0x38, REG["sp"]),
            lw(REG["s1"], 0x34, REG["sp"]),
            lw(REG["s0"], 0x30, REG["sp"]),
            lw(REG["ra"], 0x40, REG["sp"]),
            addiu(REG["sp"], REG["sp"], 0x50),
            jr(REG["ra"]),
            0,
        )
    )
    return struct.pack(f"<{len(words)}I", *words)


def build_screen_code(
    source_address: int,
    destination_address: int,
    dummy_address: int,
    status_address: int,
    original_callee: int = ORIGINAL_CALLEE,
) -> bytes:
    """Build a four-vertex screen-space calibration draw."""
    words: list[int] = [
        addiu(REG["sp"], REG["sp"], -0x50),
        sw(REG["ra"], 0x40, REG["sp"]),
        sw(REG["s0"], 0x30, REG["sp"]),
        sw(REG["s1"], 0x34, REG["sp"]),
        sw(REG["s2"], 0x38, REG["sp"]),
    ]
    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 0, REG["t0"]))
    words.append(jal(original_callee))
    words.append(0)

    load_address(words, REG["t0"], GAME_CAMERA_COUNT)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["t1"], 8, REG["t0"]))

    load_address(words, REG["t0"], GRAPHICS_DEVICE_GLOBAL)
    words.append(lw(REG["s2"], 0, REG["t0"]))
    words.append(lw(REG["s2"], 4, REG["s2"]))
    words.append(addiu(REG["a0"], REG["s2"], 0))
    load_address(words, REG["a1"], destination_address)
    load_address(words, REG["a2"], dummy_address)
    words.append(jal(GAME_IM2D_BEGIN))
    words.append(0)

    # Begin projected the known-visible dummy point in place.  Reuse its
    # screen Z for every quad vertex so Append computes the same valid rhw
    # convention as the game's own queued effects.
    load_address(words, REG["t0"], dummy_address)
    words.append(lw(REG["t1"], 8, REG["t0"]))
    load_address(words, REG["t0"], source_address)
    words.append(sw(REG["t1"], 0x08, REG["t0"]))
    words.append(sw(REG["t1"], 0x38, REG["t0"]))
    words.append(sw(REG["t1"], 0x68, REG["t0"]))
    words.append(sw(REG["t1"], 0x98, REG["t0"]))

    load_address(words, REG["s0"], source_address)
    words.append(addiu(REG["s1"], REG["zero"], 4))
    loop_index = len(words)
    words.append(addiu(REG["a0"], REG["s2"], 0))
    words.append(addiu(REG["a1"], REG["s0"], 0))
    words.append(addiu(REG["a2"], REG["s0"], 0x10))
    words.append(addiu(REG["a3"], REG["s0"], 0x20))
    words.append(jal(GAME_IM2D_APPEND))
    words.append(0)
    words.append(addiu(REG["s0"], REG["s0"], 0x30))
    words.append(addiu(REG["s1"], REG["s1"], -1))
    branch_index = len(words)
    words.append(
        bne(
            REG["s1"],
            REG["zero"],
            loop_index - (branch_index + 1),
        )
    )
    words.append(0)

    words.append(addiu(REG["a0"], REG["s2"], 0))
    words.append(addiu(REG["a1"], REG["zero"], 0))
    words.append(addiu(REG["a2"], REG["zero"], 5))
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(GAME_IM2D_FLUSH))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))
    words.extend(
        (
            lw(REG["s2"], 0x38, REG["sp"]),
            lw(REG["s1"], 0x34, REG["sp"]),
            lw(REG["s0"], 0x30, REG["sp"]),
            lw(REG["ra"], 0x40, REG["sp"]),
            addiu(REG["sp"], REG["sp"], 0x50),
            jr(REG["ra"]),
            0,
        )
    )
    return struct.pack(f"<{len(words)}I", *words)


def build_screen_blob(
    volume: dict,
    cave_address: int,
    original_callee: int = ORIGINAL_CALLEE,
) -> tuple[bytes, int, int]:
    source_address = cave_address + SCREEN_SOURCE_OFFSET
    destination_address = cave_address + VERTEX_OFFSET
    dummy_address = cave_address + SCREEN_DUMMY_OFFSET
    status_address = cave_address + STATUS_OFFSET
    code = build_screen_code(
        source_address,
        destination_address,
        dummy_address,
        status_address,
        original_callee,
    )
    if len(code) > SCREEN_SOURCE_OFFSET:
        raise ValueError(f"screen probe code is too large: 0x{len(code):X}")

    positions = (
        (100.0, 50.0, 1.0),
        (600.0, 50.0, 1.0),
        (600.0, 400.0, 1.0),
        (100.0, 400.0, 1.0),
    )
    source = bytearray(4 * 0x30)
    for index, position in enumerate(positions):
        offset = index * 0x30
        struct.pack_into("<3f", source, offset, *position)
        # GraphicsImmediate::Append consumes float RGBA in the game's native
        # 0..255 range (the effect producers feed values such as 191 and 255),
        # not normalized 0..1 colors.  The normalized calibration color was
        # therefore effectively transparent and almost black.
        struct.pack_into(
            "<4f", source, offset + 0x10, 255.0, 0.0, 0.0, 255.0
        )
        struct.pack_into("<2f", source, offset + 0x20, 0.0, 0.0)

    center = [
        volume["origin"][index] + volume["size"][index] * 0.5
        for index in range(3)
    ]
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    blob[SCREEN_SOURCE_OFFSET : SCREEN_SOURCE_OFFSET + len(source)] = source
    struct.pack_into(
        "<4f",
        blob,
        SCREEN_DUMMY_OFFSET,
        *game_to_render_position(center),
        1.0,
    )
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(blob), len(code), 4


def build_raw_3d_code(
    vertex_address: int,
    vertex_count: int,
    matrix_address: int,
    status_address: int,
    primitive_type: int,
    force_visible: bool,
    original_callee: int = ORIGINAL_CALLEE,
    preserve_original_result: bool = False,
) -> bytes:
    """Submit Im3D geometry directly inside an already-active game pass."""
    words: list[int] = [
        addiu(REG["sp"], REG["sp"], -0x40),
        sw(REG["ra"], 0x30, REG["sp"]),
    ]

    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 0, REG["t0"]))
    words.append(jal(original_callee))
    words.append(0)
    if preserve_original_result:
        # The inner-view caller consumes this function's returned camera
        # pointer immediately after the hook.
        words.append(sw(REG["v0"], 0x2C, REG["sp"]))

    # Match the game's own immediate-mode setup.  The previously tested raw
    # path used transform flags=0; every confirmed game caller uses 1.
    if force_visible:
        for state, value in RENDER_STATES:
            call_render_state(words, 0x10, state, value=value)
        call_render_state(words, 0x10, 6, value=0)
        call_render_state(words, 0x10, 20, value=1)
    else:
        call_render_state(words, 0x10, 7, value=1)
        call_render_state(words, 0x10, 1, value=0)
        call_render_state(words, 0x10, 14, value=0)

    load_address(words, REG["a0"], matrix_address)
    words.append(jal(RW_MATRIX_SET_IDENTITY))
    words.append(0)
    load_address(words, REG["a0"], vertex_address)
    words.append(addiu(REG["a1"], REG["zero"], vertex_count))
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))

    transform_failed = len(words)
    words.append(0)
    words.append(0)
    words.append(addiu(REG["a0"], REG["zero"], primitive_type))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 8, REG["t0"]))
    words.append(jal(RW_IM3D_END))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 12, REG["t0"]))
    finish = len(words)
    words[transform_failed] = beq(
        REG["v0"], REG["zero"], finish - (transform_failed + 1)
    )

    # Restore the defaults used by the game's world wrapper before returning
    # to the interrupted frame.
    call_render_state(words, 0x10, 7, value=2)
    call_render_state(words, 0x10, 1, value=0)
    if force_visible:
        call_render_state(words, 0x10, 20, value=2)
        call_render_state(words, 0x10, 8, value=1)
        call_render_state(words, 0x10, 6, value=1)

    if preserve_original_result:
        words.append(lw(REG["v0"], 0x2C, REG["sp"]))

    words.extend(
        (
            lw(REG["ra"], 0x30, REG["sp"]),
            addiu(REG["sp"], REG["sp"], 0x40),
            jr(REG["ra"]),
            0,
        )
    )
    return struct.pack(f"<{len(words)}I", *words)


def build_raw_3d_blob(
    volume: dict,
    cave_address: int,
    solid: bool,
    force_visible: bool,
    original_callee: int = ORIGINAL_CALLEE,
    preserve_original_result: bool = False,
) -> tuple[bytes, int, int]:
    vertices = bytearray(build_vertices(volume, solid=solid))
    convert_vertices_to_render_space(vertices)
    if force_visible:
        for offset in range(15, len(vertices), 36):
            vertices[offset] = 255
    vertex_address = cave_address + VERTEX_OFFSET
    matrix_offset = (VERTEX_OFFSET + len(vertices) + 15) & ~15
    matrix_address = cave_address + matrix_offset
    code = build_raw_3d_code(
        vertex_address,
        len(vertices) // 36,
        matrix_address,
        cave_address + STATUS_OFFSET,
        3 if solid else 1,
        force_visible,
        original_callee,
        preserve_original_result,
    )
    if len(code) > STATUS_OFFSET:
        raise ValueError(f"raw 3D probe code is too large: 0x{len(code):X}")
    if matrix_offset + 0x40 > MARKER_OFFSET:
        raise ValueError("raw 3D vertices do not fit in the injection blob")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    blob[VERTEX_OFFSET : VERTEX_OFFSET + len(vertices)] = vertices
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(blob), len(code), len(vertices) // 36


def build_blob(
    volume: dict,
    cave_address: int,
    solid: bool = False,
    wrapper_builder: bool = False,
    force_visible: bool = False,
    diagnose_view: bool = False,
    original_callee: int = ORIGINAL_CALLEE,
) -> tuple[bytes, int, int]:
    vertices = bytearray(build_vertices(volume, solid=solid))
    convert_vertices_to_render_space(vertices)
    if force_visible:
        for offset in range(15, len(vertices), 36):
            vertices[offset] = 255
    source_vertex_offset = SOURCE_VERTEX_OFFSET if wrapper_builder else VERTEX_OFFSET
    source_vertex_address = cave_address + source_vertex_offset
    vertex_address = cave_address + VERTEX_OFFSET
    matrix_offset = (VERTEX_OFFSET + len(vertices) + 15) & ~15
    matrix_address = cave_address + matrix_offset
    status_address = cave_address + STATUS_OFFSET
    code = build_code(
        source_vertex_address,
        vertex_address,
        len(vertices) // 36,
        matrix_address,
        status_address,
        3 if solid else 1,
        wrapper_builder,
        force_visible,
        diagnose_view,
        original_callee,
    )
    code_limit = SOURCE_VERTEX_OFFSET if wrapper_builder else STATUS_OFFSET
    if len(code) > code_limit:
        raise ValueError(f"injected code is too large: 0x{len(code):X}")
    if wrapper_builder and source_vertex_offset + len(vertices) > STATUS_OFFSET:
        raise ValueError("source vertices overlap the status block")
    if matrix_offset + 0x40 > MARKER_OFFSET:
        raise ValueError("vertices do not fit in the injection blob")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    blob[source_vertex_offset : source_vertex_offset + len(vertices)] = vertices
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(blob), len(code), len(vertices) // 36


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("volumes", type=Path, nargs="?", default=Path("research/all_city_volumes.json"))
    parser.add_argument("--volume", default="vbTrainShack")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--freecam", action="store_true")
    parser.add_argument("--freecam-speed", type=float, default=8.0)
    parser.add_argument("--mouse-sensitivity", type=float, default=0.14)
    parser.add_argument("--solid", action="store_true")
    parser.add_argument(
        "--wrapper-builder",
        action="store_true",
        help="construct the destination vertex buffer through Begin/Append",
    )
    parser.add_argument(
        "--force-visible",
        action="store_true",
        help="disable depth/culling around the probe draw and force opaque alpha",
    )
    parser.add_argument(
        "--diagnose-view",
        action="store_true",
        help="record whether view 0 accepts BeginUpdate at the hook point",
    )
    parser.add_argument(
        "--screen-test",
        action="store_true",
        help="draw a giant four-vertex 2D calibration rectangle",
    )
    parser.add_argument(
        "--raw-3d-test",
        action="store_true",
        help="submit the selected box directly through RwIm3D in the active pass",
    )
    parser.add_argument(
        "--active-pass-hook",
        action="store_true",
        help="hook the earlier 0x15671C active-pass call instead of the queue tail",
    )
    parser.add_argument(
        "--inner-view-hook",
        action="store_true",
        help="hook 0x185DEC inside the confirmed HUD/orthographic camera pass",
    )
    parser.add_argument(
        "--main-view-hook",
        action="store_true",
        help="hook 0x185BFC inside the confirmed main 3D camera pass",
    )
    parser.add_argument(
        "--camera-test-box",
        action="store_true",
        help="replace the selected volume with a large box in front of the live camera",
    )
    parser.add_argument("--camera-test-distance", type=float, default=12.0)
    parser.add_argument("--camera-test-size", type=float, default=12.0)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument(
        "--install-only",
        action="store_true",
        help="install the reversible render hook, print its heap pointer, and exit without cleanup",
    )
    parser.add_argument(
        "--cleanup-pointer",
        type=lambda value: int(value, 0),
        help="restore the stable hook and release a prior --install-only heap block",
    )
    args = parser.parse_args()

    if args.screen_test and args.raw_3d_test:
        raise ValueError("--screen-test and --raw-3d-test are mutually exclusive")
    alternate_hooks = sum(
        bool(value)
        for value in (
            args.active_pass_hook,
            args.inner_view_hook,
            args.main_view_hook,
        )
    )
    if alternate_hooks > 1:
        raise ValueError("choose only one alternate render hook")
    if (args.inner_view_hook or args.main_view_hook) and not args.raw_3d_test:
        raise ValueError("view hooks currently require --raw-3d-test")
    if args.raw_3d_test and not (
        args.active_pass_hook or args.inner_view_hook or args.main_view_hook
    ):
        raise ValueError(
            "--raw-3d-test requires an active-pass or view hook"
        )

    if args.main_view_hook:
        hook_address = MAIN_VIEW_HOOK_ADDRESS
        hook_original = MAIN_VIEW_HOOK_ORIGINAL
        original_callee = MAIN_VIEW_ORIGINAL_CALLEE
    elif args.inner_view_hook:
        hook_address = INNER_VIEW_HOOK_ADDRESS
        hook_original = INNER_VIEW_HOOK_ORIGINAL
        original_callee = INNER_VIEW_ORIGINAL_CALLEE
    elif args.active_pass_hook:
        hook_address = ACTIVE_PASS_HOOK_ADDRESS
        hook_original = ACTIVE_PASS_HOOK_ORIGINAL
        original_callee = ACTIVE_PASS_ORIGINAL_CALLEE
    else:
        hook_address = HOOK_ADDRESS
        hook_original = HOOK_ORIGINAL
        original_callee = ORIGINAL_CALLEE

    document = json.loads(args.volumes.read_text(encoding="utf-8"))
    volume = find_volume(document, args.volume)

    client: PineClient | None = PineClient(port=args.port)
    allocated = 0
    installed = False
    try:
        assert client is not None
        if client.text(GAME_ID) != "SLUS-21215":
            raise RuntimeError("The Warriors SLUS-21215 is not running")
        if args.camera_test_box:
            if args.camera_test_distance <= 0 or args.camera_test_size <= 0:
                raise ValueError("camera test distance and size must be positive")
            volume = build_camera_test_volume(
                client, args.camera_test_distance, args.camera_test_size
            )
        if args.screen_test:
            _preview, code_size, vertex_count = build_screen_blob(
                volume, 0x00800000, original_callee
            )
        elif args.raw_3d_test:
            _preview, code_size, vertex_count = build_raw_3d_blob(
                volume,
                0x00800000,
                args.solid,
                args.force_visible,
                original_callee,
                args.inner_view_hook or args.main_view_hook,
            )
        else:
            _preview, code_size, vertex_count = build_blob(
                volume,
                0x00800000,
                args.solid,
                args.wrapper_builder,
                args.force_visible,
                args.diagnose_view,
                original_callee,
            )
        current_hook = client.read(hook_address, 32)
        if args.cleanup_pointer is not None:
            marker = client.read_block(
                args.cleanup_pointer + MARKER_OFFSET, 16, progress=False
            )
            if marker != MARKER:
                raise RuntimeError(
                    f"probe marker mismatch at 0x{args.cleanup_pointer:08X}"
                )
            client.write(hook_address, hook_original, 32)
            run_heap_bootstrap(client, args.cleanup_pointer, freeing=True)
            print(
                f"cleaned render hook and released EE heap block "
                f"0x{args.cleanup_pointer:08X}"
            )
            return
        if args.restore:
            client.write(hook_address, hook_original, 32)
            print(f"render hook 0x{hook_address:08X} restored")
            return
        if current_hook != hook_original:
            raise RuntimeError(f"hook mismatch: 0x{current_hook:08X}")
        print(
            f"game={client.text(TITLE)} volume={volume['name']} "
            f"hook=0x{hook_address:08X} code=0x{code_size:X} "
            f"vertices={vertex_count}"
        )
        if args.dry_run:
            print("dry-run: validation passed; no memory was changed")
            return

        allocated = run_heap_bootstrap(client, BLOB_SIZE)
        if not 0x00700000 <= allocated < 0x01FF0000:
            raise RuntimeError(f"unexpected EE heap pointer: 0x{allocated:08X}")
        if args.screen_test:
            blob, _, _ = build_screen_blob(
                volume, allocated, original_callee
            )
        elif args.raw_3d_test:
            blob, _, _ = build_raw_3d_blob(
                volume,
                allocated,
                args.solid,
                args.force_visible,
                original_callee,
                args.inner_view_hook or args.main_view_hook,
            )
        else:
            blob, _, _ = build_blob(
                volume,
                allocated,
                args.solid,
                args.wrapper_builder,
                args.force_visible,
                args.diagnose_view,
                original_callee,
            )
        client.write_block(allocated, blob)
        client.write(hook_address, jal(allocated), 32)
        installed = True
        print(f"game-wrapper box active at 0x{allocated:08X}", flush=True)

        if args.install_only:
            print(f"ACTIVE_POINTER=0x{allocated:08X}", flush=True)
            # Ownership is deliberately transferred to --cleanup-pointer.
            installed = False
            allocated = 0
            return

        if args.freecam:
            # PCSX2 services only one active PINE client reliably.  Release
            # this connection and run the exact standalone freecam that has
            # been validated interactively, then reconnect for cleanup.
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
                ],
                check=True,
            )
            client = PineClient(port=args.port)
        else:
            deadline = time.monotonic() + args.duration if args.duration > 0 else float("inf")
            while time.monotonic() < deadline:
                time.sleep(0.02)
        assert client is not None
        status = client.read_block(allocated + STATUS_OFFSET, 16, progress=False)
        executions, flush_result, camera_count, begin_update = struct.unpack(
            "<4I", status
        )
        if args.screen_test:
            projected = struct.unpack(
                "<4f",
                client.read_block(
                    allocated + SCREEN_DUMMY_OFFSET, 16, progress=False
                ),
            )
            print(
                "projected-dummy="
                f"({projected[0]:.6f}, {projected[1]:.6f}, "
                f"{projected[2]:.6f}, {projected[3]:.6f})"
            )
        if args.raw_3d_test:
            live_vertices = client.read_block(
                allocated + VERTEX_OFFSET,
                min(vertex_count, 4) * 36,
                progress=False,
            )
            original_vertices = blob[
                VERTEX_OFFSET : VERTEX_OFFSET + len(live_vertices)
            ]
            for index in range(min(vertex_count, 4)):
                offset = index * 36
                original_position = struct.unpack_from(
                    "<3f", original_vertices, offset
                )
                live_position = struct.unpack_from("<3f", live_vertices, offset)
                print(
                    f"raw-vertex[{index}] original="
                    f"({original_position[0]:.6f}, {original_position[1]:.6f}, "
                    f"{original_position[2]:.6f}) live="
                    f"({live_position[0]:.6f}, {live_position[1]:.6f}, "
                    f"{live_position[2]:.6f})"
                )
        if args.raw_3d_test:
            print(
                f"render-status executions={executions} "
                f"transform=0x{flush_result:08X} "
                f"primitive=0x{camera_count:08X} end=0x{begin_update:08X}"
            )
        else:
            print(
                f"render-status executions={executions} "
                f"flush=0x{flush_result:08X} cameras={camera_count} "
                f"begin_update={begin_update}"
            )
    except KeyboardInterrupt:
        pass
    finally:
        if client is None:
            try:
                client = PineClient(port=args.port)
            except Exception as error:
                print(f"WARNING: could not reconnect for cleanup: {error}")
        if installed:
            try:
                if client is None:
                    raise RuntimeError("PINE is unavailable")
                client.write(hook_address, hook_original, 32)
                print(f"original render hook 0x{hook_address:08X} restored")
            except Exception as error:
                print(f"WARNING: could not restore hook: {error}")
        if allocated:
            try:
                if client is None:
                    raise RuntimeError("PINE is unavailable")
                run_heap_bootstrap(client, allocated, freeing=True)
                print(f"released EE heap block 0x{allocated:08X}")
            except Exception as error:
                print(f"WARNING: could not release EE heap block: {error}")
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
