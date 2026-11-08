#!/usr/bin/env python3
"""Render one map's complete extracted volume set in two 0x1000 EE blocks.

The accepted All-City tool remains on ``all_volumes_probe.py``.  This variant
keeps the same proven render hook but quantizes precomputed corners to signed
16-bit coordinates.  Even the largest known map (81 boxes) fits in the game's
proven two-block heap limit.
"""

from __future__ import annotations

import argparse
import json
import struct
import time
from collections import Counter
from pathlib import Path

from all_volumes_probe import (
    DESTINATION_OFFSET,
    GET_HUMAN_POSITION,
    GET_PLAYER,
    MATRIX_OFFSET,
    PLAYER_POSITION_OFFSET,
    S3,
    S4,
    STATUS_OFFSET,
    T2,
    T3,
    T4,
    T5,
    VERTICES_PER_SOLID_BOX,
    addu,
    compact_box,
    i_type,
    lbu,
    select_volumes,
    sll,
    validate_clean_state,
)
from freecam_probe import (
    CAMERA_OVERRIDE_CAMERA_POINTER_OFFSET,
    CAMERA_OVERRIDE_FLAG_OFFSET,
    CAMERA_OVERRIDE_MATRIX_OFFSET,
    CAMERA_OVERRIDE_STUB_OFFSET,
    CAMERA_STABLE_SECONDS,
    wait_for_camera_transform,
)
from ingame_volume_probe import (
    BLOB_SIZE,
    KIND_COLORS,
    MARKER_OFFSET,
    REG,
    RW_IM3D_END,
    RW_IM3D_RENDER_PRIMITIVE,
    RW_IM3D_TRANSFORM,
    RW_MATRIX_SET_IDENTITY,
    addiu,
    beq,
    bne,
    call_render_state,
    jal,
    jr,
    load_address,
    lw,
    run_heap_free_pair_bootstrap,
    run_heap_bootstrap,
    sw,
)
from ingame_wrapper_probe import (
    MAIN_VIEW_HOOK_ADDRESS,
    MAIN_VIEW_HOOK_ORIGINAL,
    MAIN_VIEW_ORIGINAL_CALLEE,
)
from pine_client import GAME_ID, TITLE, PineClient


MAP_CODE_MARKER = b"TW_MAP_CODE_V1\0\0"
MAP_DATA_MARKER = b"TW_MAP_DATA_V1\0\0"
CAMERA_BEGIN_UPDATE_HOOK = 0x00198948
CAMERA_BEGIN_UPDATE_ORIGINAL = 0x0C1209EE
CAMERA_BEGIN_UPDATE_CALLEE = 0x004827B8
DATA_META_OFFSET = STATUS_OFFSET + 20
QUANTIZED_RECORD_SIZE = 8 * 3 * 2
QUANTIZED_LIMIT = 32760
KIND_SLOT_COUNT = 10
F0 = 0
F1 = 1


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def lh(rt: int, offset: int, base: int) -> int:
    return i_type(0x21, base, rt, offset)


def lwc1(ft: int, offset: int, base: int) -> int:
    return i_type(0x31, base, ft, offset)


def swc1(ft: int, offset: int, base: int) -> int:
    return i_type(0x39, base, ft, offset)


def lq(rt: int, offset: int, base: int) -> int:
    return i_type(0x1E, base, rt, offset)


def sq(rt: int, offset: int, base: int) -> int:
    return i_type(0x1F, base, rt, offset)


def build_camera_override_stub(code_address: int) -> bytes:
    """Override the main 3D camera after RenderWare finishes BeginUpdate."""
    words: list[int] = [
        addiu(REG["sp"], REG["sp"], -0x20),
        sw(REG["ra"], 0x10, REG["sp"]),
        jal(CAMERA_BEGIN_UPDATE_CALLEE),
        0,
    ]
    failed = len(words)
    words.append(0)
    load_address(words, REG["t1"], code_address)
    words.append(lw(T2, CAMERA_OVERRIDE_FLAG_OFFSET, REG["t1"]))
    disabled = len(words)
    words.extend(
        (
            0,
            lw(
                REG["t0"],
                CAMERA_OVERRIDE_CAMERA_POINTER_OFFSET,
                REG["t1"],
            ),
        )
    )
    wrong_camera = len(words)
    words.extend(
        (
            0,
            addiu(
                REG["t1"],
                REG["t1"],
                CAMERA_OVERRIDE_MATRIX_OFFSET,
            ),
        )
    )
    for offset in range(0, 0x40, 0x10):
        words.extend(
            (
                lq(T2, offset, REG["t1"]),
                sq(
                    T2,
                    0x20 + offset,
                    REG["a0"],
                ),
            )
        )
    finished = len(words)
    words.extend(
        (
            lw(REG["ra"], 0x10, REG["sp"]),
            addiu(REG["sp"], REG["sp"], 0x20),
            jr(REG["ra"]),
            0,
        )
    )
    words[failed] = beq(
        REG["v0"],
        REG["zero"],
        finished - (failed + 1),
    )
    words[disabled] = beq(
        T2,
        REG["zero"],
        finished - (disabled + 1),
    )
    words[wrong_camera] = bne(
        REG["a0"],
        REG["t0"],
        finished - (wrong_camera + 1),
    )
    stub = struct.pack(f"<{len(words)}I", *words)
    if len(stub) > CAMERA_OVERRIDE_MATRIX_OFFSET - CAMERA_OVERRIDE_STUB_OFFSET:
        raise ValueError("native camera-override stub exceeds its reserved gap")
    return stub


def mtc1(rt: int, fs: int) -> int:
    return (0x11 << 26) | (0x04 << 21) | (rt << 16) | (fs << 11)


def cvt_s_w(fd: int, fs: int) -> int:
    return (0x11 << 26) | (0x14 << 21) | (fs << 11) | (fd << 6) | 0x20


def mul_s(fd: int, fs: int, ft: int) -> int:
    return (
        (0x11 << 26)
        | (0x10 << 21)
        | (ft << 16)
        | (fs << 11)
        | (fd << 6)
        | 0x02
    )


def build_quantized_data(
    volumes: list[dict],
) -> tuple[bytes, int, int, int, int, int, int, float]:
    float_records: list[list[float]] = []
    shared_indices: bytes | None = None
    for volume in volumes:
        record, indices = compact_box(volume)
        coordinates = list(struct.unpack("<24f", record[:96]))
        float_records.append(coordinates)
        if shared_indices is None:
            shared_indices = indices
        elif indices != shared_indices:
            raise ValueError("solid boxes do not share one triangle topology")
    if shared_indices is None:
        raise ValueError("at least one volume is required")

    maximum = max(abs(value) for record in float_records for value in record)
    scale = max(maximum / QUANTIZED_LIMIT, 1.0 / QUANTIZED_LIMIT)
    records = bytearray()
    max_error = 0.0
    for record in float_records:
        for value in record:
            quantized = round(value / scale)
            if not -32768 <= quantized <= 32767:
                raise ValueError("quantized corner exceeds signed 16-bit range")
            records.extend(struct.pack("<h", quantized))
            max_error = max(max_error, abs(value - quantized * scale))

    index_offset = len(records)
    kind_offset = index_offset + len(shared_indices)
    color_offset = align_up(kind_offset + len(volumes), 4)
    visibility_offset = color_offset + KIND_SLOT_COUNT * 4
    scale_offset = align_up(visibility_offset + KIND_SLOT_COUNT, 4)
    payload_end = scale_offset + 4
    marker_offset = MARKER_OFFSET
    if payload_end > marker_offset:
        raise ValueError(
            f"quantized volume data exceed one heap block: 0x{payload_end:X}"
        )

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
    for kind in range(KIND_SLOT_COUNT):
        red, green, blue, _alpha = KIND_COLORS.get(
            kind, (255, 255, 255, 255)
        )
        struct.pack_into("<4B", blob, color_offset + kind * 4, red, green, blue, 72)
    blob[
        visibility_offset : visibility_offset + KIND_SLOT_COUNT
    ] = bytes([1]) * KIND_SLOT_COUNT
    struct.pack_into("<f", blob, scale_offset, scale)
    blob[marker_offset : marker_offset + len(MAP_DATA_MARKER)] = MAP_DATA_MARKER
    return (
        bytes(blob),
        index_offset,
        kind_offset,
        color_offset,
        visibility_offset,
        scale_offset,
        payload_end,
        max_error,
    )


def build_quantized_render_code(
    code_address: int,
    data_address: int,
    index_address: int,
    kind_address: int,
    color_address: int,
    visibility_address: int,
    scale_address: int,
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
    words.extend(
        (
            lw(REG["t1"], 0, REG["t0"]),
            addiu(REG["t1"], REG["t1"], 1),
            sw(REG["t1"], 0, REG["t0"]),
            jal(MAIN_VIEW_ORIGINAL_CALLEE),
            0,
            sw(REG["v0"], 0x34, REG["sp"]),
            addiu(REG["a0"], REG["zero"], 0),
            jal(GET_PLAYER),
            0,
            addiu(REG["a0"], REG["v0"], 0),
            jal(GET_HUMAN_POSITION),
            0,
        )
    )
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

    call_render_state(words, 0x10, 7, value=1)
    call_render_state(words, 0x10, 1, value=0)
    call_render_state(words, 0x10, 14, value=0)
    load_address(words, REG["a0"], matrix_address)
    words.extend((jal(RW_MATRIX_SET_IDENTITY), 0))

    load_address(words, REG["s0"], data_address)
    words.append(addiu(REG["s1"], REG["zero"], volume_count))
    load_address(words, S3, index_address)
    load_address(words, S4, kind_address)
    outer_loop = len(words)

    words.append(lbu(T5, 0, S4))
    load_address(words, T2, visibility_address)
    words.append(addu(T2, T2, T5))
    words.append(lbu(T2, 0, T2))
    hidden = len(words)
    words.extend((0, 0))
    words.append(sll(T5, T5, 2))
    load_address(words, T2, color_address)
    words.append(addu(T5, T5, T2))
    words.append(lw(T5, 0, T5))
    load_address(words, T2, scale_address)
    words.append(lwc1(F1, 0, T2))

    load_address(words, REG["s2"], destination_address)
    words.append(addiu(REG["t0"], S3, 0))
    words.append(addiu(REG["t1"], REG["zero"], VERTICES_PER_SOLID_BOX))
    inner_loop = len(words)
    words.extend(
        (
            lbu(T2, 0, REG["t0"]),
            sll(T3, T2, 2),
            sll(T4, T2, 1),
            addu(T3, T3, T4),
            addu(T3, REG["s0"], T3),
        )
    )
    for source_offset, destination_offset in ((0, 0), (2, 4), (4, 8)):
        words.extend(
            (
                lh(T4, source_offset, T3),
                mtc1(T4, F0),
                cvt_s_w(F0, F0),
                mul_s(F0, F0, F1),
                swc1(F0, destination_offset, REG["s2"]),
            )
        )
    words.extend(
        (
            sw(T5, 12, REG["s2"]),
            addiu(REG["t0"], REG["t0"], 1),
            addiu(REG["s2"], REG["s2"], 36),
            addiu(REG["t1"], REG["t1"], -1),
        )
    )
    inner_branch = len(words)
    words.extend(
        (
            bne(REG["t1"], REG["zero"], inner_loop - (inner_branch + 1)),
            0,
        )
    )

    load_address(words, REG["a0"], destination_address)
    words.append(addiu(REG["a1"], REG["zero"], VERTICES_PER_SOLID_BOX))
    load_address(words, REG["a2"], matrix_address)
    words.extend(
        (
            addiu(REG["a3"], REG["zero"], 1),
            jal(RW_IM3D_TRANSFORM),
            0,
        )
    )
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))
    failed = len(words)
    words.extend((0, 0))

    words.extend(
        (
            addiu(REG["a0"], REG["zero"], 3),
            jal(RW_IM3D_RENDER_PRIMITIVE),
            0,
        )
    )
    load_address(words, REG["t0"], status_address)
    words.extend((sw(REG["v0"], 8, REG["t0"]), jal(RW_IM3D_END), 0))
    load_address(words, REG["t0"], status_address)
    words.extend(
        (
            sw(REG["v0"], 12, REG["t0"]),
            lw(REG["t1"], 16, REG["t0"]),
            addiu(REG["t1"], REG["t1"], 1),
            sw(REG["t1"], 16, REG["t0"]),
        )
    )
    box_end = len(words)
    words[failed] = beq(REG["v0"], REG["zero"], box_end - (failed + 1))
    words[hidden] = beq(T2, REG["zero"], box_end - (hidden + 1))
    words.extend(
        (
            addiu(REG["s0"], REG["s0"], QUANTIZED_RECORD_SIZE),
            addiu(S4, S4, 1),
            addiu(REG["s1"], REG["s1"], -1),
        )
    )
    outer_branch = len(words)
    words.extend(
        (
            bne(REG["s1"], REG["zero"], outer_loop - (outer_branch + 1)),
            0,
        )
    )

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


def build_map_code_block(
    code_address: int,
    data_address: int,
    index_address: int,
    kind_address: int,
    color_address: int,
    visibility_address: int,
    scale_address: int,
    volume_count: int,
) -> tuple[bytes, int]:
    code = build_quantized_render_code(
        code_address,
        data_address,
        index_address,
        kind_address,
        color_address,
        visibility_address,
        scale_address,
        volume_count,
    )
    if len(code) > PLAYER_POSITION_OFFSET:
        raise ValueError(f"multi-volume render code is too large: 0x{len(code):X}")
    destination_end = DESTINATION_OFFSET + VERTICES_PER_SOLID_BOX * 36
    if destination_end > STATUS_OFFSET:
        raise ValueError("reusable RwIm3D buffer overlaps status data")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    camera_override_stub = build_camera_override_stub(code_address)
    blob[
        CAMERA_OVERRIDE_STUB_OFFSET
        : CAMERA_OVERRIDE_STUB_OFFSET + len(camera_override_stub)
    ] = camera_override_stub
    struct.pack_into(
        "<III",
        blob,
        DATA_META_OFFSET,
        volume_count,
        MARKER_OFFSET,
        BLOB_SIZE,
    )
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MAP_CODE_MARKER)] = MAP_CODE_MARKER
    return bytes(blob), len(code)


def validate_layout(volumes: list[dict]) -> tuple[int, int, int, float]:
    (
        data_blob,
        index_offset,
        kind_offset,
        color_offset,
        visibility_offset,
        scale_offset,
        payload_end,
        max_error,
    ) = build_quantized_data(volumes)
    _code_blob, code_size = build_map_code_block(
        0x00800000,
        0x00900000,
        0x00900000 + index_offset,
        0x00900000 + kind_offset,
        0x00900000 + color_offset,
        0x00900000 + visibility_offset,
        0x00900000 + scale_offset,
        len(volumes),
    )
    return code_size, len(data_blob), payload_end, max_error


def disarm_renderer(client: PineClient, code_pointer: int) -> None:
    """Restore renderer hooks without invoking the game heap allocator."""
    current_hook = client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
    installed_hook = jal(code_pointer)
    if current_hook not in (MAIN_VIEW_HOOK_ORIGINAL, installed_hook):
        raise RuntimeError(f"unexpected main-view hook: 0x{current_hook:08X}")
    current_camera_hook = client.read(CAMERA_BEGIN_UPDATE_HOOK, 32)
    installed_camera_hook = jal(
        code_pointer + CAMERA_OVERRIDE_STUB_OFFSET
    )
    if current_camera_hook not in (
        CAMERA_BEGIN_UPDATE_ORIGINAL,
        installed_camera_hook,
    ):
        raise RuntimeError(
            f"unexpected camera BeginUpdate hook: 0x{current_camera_hook:08X}"
        )
    client.write(
        CAMERA_BEGIN_UPDATE_HOOK,
        CAMERA_BEGIN_UPDATE_ORIGINAL,
        32,
    )
    client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
    if (
        client.read(CAMERA_BEGIN_UPDATE_HOOK, 32)
        != CAMERA_BEGIN_UPDATE_ORIGINAL
        or client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
        != MAIN_VIEW_HOOK_ORIGINAL
    ):
        raise RuntimeError("renderer hook restoration did not verify")


def cleanup_renderer(
    client: PineClient,
    code_pointer: int,
    data_pointer: int,
    *,
    if_owned: bool = False,
) -> bool:
    """Disarm and release owned blocks; return whether they were released."""
    disarm_renderer(client, code_pointer)
    code_marker = client.read_block(
        code_pointer + MARKER_OFFSET, len(MAP_CODE_MARKER), progress=False
    )
    if if_owned and code_marker != MAP_CODE_MARKER:
        print(
            "map-volume renderer blocks were reclaimed or overwritten; "
            "no heap free was attempted",
            flush=True,
        )
        return False
    if code_marker != MAP_CODE_MARKER:
        raise RuntimeError("map-volume code marker mismatch")
    metadata_address = code_pointer + DATA_META_OFFSET
    metadata_base = metadata_address & ~7
    metadata = client.read_block(metadata_base, 16, progress=False)
    volume_count, data_marker_offset, data_size = struct.unpack_from(
        "<III", metadata, metadata_address - metadata_base
    )
    metadata_valid = (
        volume_count > 0
        and data_size == BLOB_SIZE
        and data_marker_offset == data_size - len(MAP_DATA_MARKER)
    )
    if if_owned and not metadata_valid:
        print(
            "map-volume allocation metadata were reclaimed or overwritten; "
            "no heap free was attempted",
            flush=True,
        )
        return False
    if not metadata_valid:
        raise RuntimeError("map-volume allocation metadata are invalid")
    data_marker = client.read_block(
        data_pointer + data_marker_offset,
        len(MAP_DATA_MARKER),
        progress=False,
    )
    if if_owned and data_marker != MAP_DATA_MARKER:
        print(
            "map-volume data block was reclaimed or overwritten; "
            "no heap free was attempted",
            flush=True,
        )
        return False
    if data_marker != MAP_DATA_MARKER:
        raise RuntimeError("map-volume data marker mismatch")
    run_heap_free_pair_bootstrap(client, data_pointer, code_pointer)
    print(
        f"cleaned map-volume renderer code=0x{code_pointer:08X} "
        f"data=0x{data_pointer:08X} volumes={volume_count}"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("volumes", type=Path)
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--max-distance", type=float, default=0.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument(
        "--wait-for-camera",
        action="store_true",
        help="wait read-only until any compatible mission camera is active",
    )
    parser.add_argument(
        "--camera-wait-timeout",
        type=float,
        default=0.0,
        help="seconds to wait for the gameplay camera; zero waits indefinitely",
    )
    parser.add_argument(
        "--camera-stable-seconds",
        type=float,
        default=CAMERA_STABLE_SECONDS,
        help="continuous compatible-camera time required before proceeding",
    )
    parser.add_argument(
        "--cleanup-code-pointer", type=lambda value: int(value, 0)
    )
    parser.add_argument(
        "--cleanup-data-pointer", type=lambda value: int(value, 0)
    )
    parser.add_argument(
        "--cleanup-if-owned",
        action="store_true",
        help="skip heap frees if transition-time ownership markers disappeared",
    )
    parser.add_argument(
        "--disarm-only",
        action="store_true",
        help="restore renderer hooks without touching the game heap",
    )
    args = parser.parse_args()

    if (
        args.max_distance < 0
        or args.limit < 0
        or args.camera_wait_timeout < 0
        or args.camera_stable_seconds < 0
    ):
        raise ValueError(
            "max-distance, limit, camera-wait-timeout, and "
            "camera-stable-seconds cannot be negative"
        )
    cleanup_requested = (
        args.cleanup_code_pointer is not None
        or args.cleanup_data_pointer is not None
    )
    if cleanup_requested and not args.disarm_only and (
        args.cleanup_code_pointer is None
        or args.cleanup_data_pointer is None
    ):
        raise ValueError("cleanup requires both code and data pointers")
    if args.install_only and cleanup_requested:
        raise ValueError("install-only and cleanup are mutually exclusive")
    if args.cleanup_if_owned and not (
        args.cleanup_code_pointer is not None
        and args.cleanup_data_pointer is not None
    ):
        raise ValueError("cleanup-if-owned requires both cleanup pointers")
    if args.disarm_only and (
        args.cleanup_code_pointer is None
        or args.cleanup_data_pointer is not None
        or args.install_only
        or args.validate_only
        or args.wait_for_camera
    ):
        raise ValueError(
            "disarm-only requires only --cleanup-code-pointer"
        )
    if args.wait_for_camera and (
        args.validate_only or args.install_only or cleanup_requested
    ):
        raise ValueError(
            "wait-for-camera must be used separately from validation, "
            "installation, or cleanup"
        )
    if not (
        args.validate_only
        or args.install_only
        or args.wait_for_camera
        or cleanup_requested
    ) and args.duration <= 0:
        raise ValueError("duration must be positive")

    document = json.loads(args.volumes.read_text(encoding="utf-8"))
    all_volumes = document.get("volumes", [])
    if document.get("count") != len(all_volumes) or not all_volumes:
        raise ValueError("volume dataset count is missing or inconsistent")

    if args.validate_only:
        if args.max_distance:
            raise ValueError("validate-only cannot apply max-distance without a live camera")
        selected = all_volumes[: args.limit or None]
        code_size, data_size, payload_size, max_error = validate_layout(selected)
        print(
            f"validated {len(selected)} volumes code=0x{code_size:X} "
            f"data-allocation=0x{data_size:X} payload=0x{payload_size:X} "
            f"max-error={max_error:.5f}"
        )
        return

    client: PineClient | None = PineClient(port=args.port)
    allocations: list[int] = []
    hook_installed = False
    try:
        if client.text(GAME_ID) != "SLUS-21215":
            raise RuntimeError("The Warriors SLUS-21215 is not running")
        if args.wait_for_camera:
            transform = wait_for_camera_transform(
                client,
                timeout=args.camera_wait_timeout,
                stable_duration=args.camera_stable_seconds,
            )
            print(f"camera-ready transform=0x{transform:08X}", flush=True)
            return
        if args.disarm_only:
            assert args.cleanup_code_pointer is not None
            disarm_renderer(client, args.cleanup_code_pointer)
            print(
                "disarmed map-volume renderer "
                f"code=0x{args.cleanup_code_pointer:08X}",
                flush=True,
            )
            return
        if cleanup_requested:
            cleanup_renderer(
                client,
                args.cleanup_code_pointer,
                args.cleanup_data_pointer,
                if_owned=args.cleanup_if_owned,
            )
            return

        validate_clean_state(client)
        transform = wait_for_camera_transform(
            client,
            timeout=args.camera_wait_timeout,
            stable_duration=args.camera_stable_seconds,
        )
        camera_position = struct.unpack_from(
            "<3f", client.read_block(transform, 16, progress=False)
        )
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
            color_offset,
            visibility_offset,
            scale_offset,
            payload_end,
            max_error,
        ) = build_quantized_data(selected)
        _preview, code_size = build_map_code_block(
            0x00800000,
            0x00900000,
            0x00900000 + index_offset,
            0x00900000 + kind_offset,
            0x00900000 + color_offset,
            0x00900000 + visibility_offset,
            0x00900000 + scale_offset,
            len(selected),
        )
        print(
            f"game={client.text(TITLE)} selected={len(selected)} "
            f"blocks=2 code=0x{code_size:X} data-allocation=0x{len(data_blob):X} "
            f"payload=0x{payload_end:X} max-error={max_error:.5f}"
        )
        for kind, count in sorted(Counter(v["kind"] for v in selected).items()):
            red, green, blue, _alpha = KIND_COLORS.get(
                kind, (255, 255, 255, 255)
            )
            print(f"  kind={kind} count={count} rgb=({red},{green},{blue})")

        code_address = run_heap_bootstrap(client, BLOB_SIZE)
        allocations.append(code_address)
        data_address = run_heap_bootstrap(client, len(data_blob))
        if data_address in allocations:
            raise RuntimeError(f"duplicate EE heap pointer: 0x{data_address:08X}")
        allocations.append(data_address)
        if not (
            0x00700000 <= code_address < 0x01FF0000
            and 0x00700000 <= data_address
            and data_address + len(data_blob) < 0x01FFF000
        ):
            raise RuntimeError("one or more EE heap blocks are outside the expected range")

        client.write_block(data_address, data_blob)
        code_blob, _ = build_map_code_block(
            code_address,
            data_address,
            data_address + index_offset,
            data_address + kind_offset,
            data_address + color_offset,
            data_address + visibility_offset,
            data_address + scale_offset,
            len(selected),
        )
        client.write_block(code_address, code_blob)
        client.write(MAIN_VIEW_HOOK_ADDRESS, jal(code_address), 32)
        hook_installed = True
        if client.read(MAIN_VIEW_HOOK_ADDRESS, 32) != jal(code_address):
            raise RuntimeError("map renderer hook installation did not verify")
        print(
            f"map-volume render active code=0x{code_address:08X} "
            f"data=0x{data_address:08X}",
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
                f"ACTIVE_PLAYER_POSITION_POINTER="
                f"0x{code_address + PLAYER_POSITION_OFFSET:08X}",
                flush=True,
            )
            hook_installed = False
            allocations.clear()
            return

        time.sleep(args.duration)
        client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
        hook_installed = False
        status = struct.unpack(
            "<6I",
            client.read_block(code_address + STATUS_OFFSET, 24, progress=False),
        )
        print(
            f"render-status executions={status[0]} transform=0x{status[1]:08X} "
            f"primitive={status[2]} end={status[3]} "
            f"volume-draws={status[4]} volumes={status[5]}"
        )
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_safe = client is not None
        if hook_installed and client is not None:
            try:
                client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
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
                    print(f"WARNING: could not release 0x{address:08X}: {error}")
        elif allocations:
            print("WARNING: retained heap blocks because hook cleanup was not verified")
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
