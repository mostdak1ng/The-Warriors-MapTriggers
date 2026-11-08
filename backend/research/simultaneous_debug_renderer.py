#!/usr/bin/env python3
"""Render one map's triggers, flags, and nearby terrain simultaneously.

The stable renderer keeps its two reversible 0x1000-byte allocations.  The
explicit experimental OBB mode grows only the data allocation to 0x1780 bytes,
leaving the stable layout untouched.  Trigger flags are compact quantized
octahedra, terrain faces are references to live collision vertices, and live
object OBBs are host-updated corner records expanded by the same native hook.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import time
from pathlib import Path

import all_volumes_probe as trigger_probe
import collision_render_probe as collision_probe
import map_volumes_probe as map_trigger_probe
from freecam_probe import (
    CAMERA_OVERRIDE_FLAG_OFFSET,
    CAMERA_OVERRIDE_MATRIX_OFFSET,
    CAMERA_OVERRIDE_STUB_OFFSET,
    WorldTimeController,
    resolve_camera_transform,
)
from ingame_volume_probe import (
    BLOB_SIZE,
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
    call_render_state,
    jal,
    jr,
    load_address,
    lui,
    lw,
    probe_heap_callsite,
    BootstrapNotEntered, BootstrapUncertain, BootstrapCancelled,
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
import native_freecam
from player_air_stand import (
    EMBEDDED_END_OFFSET as AIR_STAND_END_OFFSET,
    EMBEDDED_STUB_OFFSETS as AIR_STAND_STUB_OFFSETS,
    write_embedded_layout as write_air_stand_layout,
)


TERRAIN_RECORD_OFFSET = 0x540
TERRAIN_RECORD_SIZE = 16
DESTINATION_OFFSET = 0x800
STATUS_OFFSET = 0xFC0
MATRIX_OFFSET = 0xF80
TERRAIN_COUNT_STATUS_OFFSET = 0x18
TERRAIN_CAPACITY = (
    DESTINATION_OFFSET - TERRAIN_RECORD_OFFSET
) // TERRAIN_RECORD_SIZE
PLAYER_DEBUG_HEADER_OFFSET = 0xD10
PLAYER_DEBUG_POINT_OFFSET = 0xD20
PLAYER_DEBUG_LINE_OFFSET = 0xE20
PLAYER_DEBUG_MAX_POINTS = 20
PLAYER_DEBUG_MAX_LINES = 18
PLAYER_DEBUG_POINT_SIZE = 12
PLAYER_DEBUG_LINE_SIZE = 8
EXTENDED_DATA_SIZE = 0x1780
EXTENDED_TERRAIN_RECORD_OFFSET = 0x1000
OBB_POINT_RECORD_OFFSET = 0x12C0
OBB_POINT_RECORD_SIZE = 8 * 6
OBB_CAPACITY = 24
OBB_INDEX_OFFSET = 0x1740
OBB_COLOR_OFFSET = 0x1758
OBB_SCALE_OFFSET = 0x175C
OBB_COORD_SCALE = 1.0 / 32.0
OBB_COUNT_STATUS_OFFSET = 0x1C
OBB_VERTICES_PER_BOX = 24
VERTICES_PER_SOLID_BOX = trigger_probe.VERTICES_PER_SOLID_BOX
VERTICES_PER_TRIANGLE = 3
TRIGGER_RECORD_SIZE = map_trigger_probe.QUANTIZED_RECORD_SIZE
KIND_SLOT_COUNT = trigger_probe.KIND_SLOT_COUNT
DATA_MARKER = map_trigger_probe.MAP_DATA_MARKER
FLAG_RENDER_MAGIC = b"TWFG"
FLAG_RENDER_HEADER = struct.Struct("<4sBBB1x")
FLAG_RENDER_HEADER_OFFSET = STATUS_OFFSET + 0x20
VOLUME_SELECTION_OFFSET = FLAG_RENDER_HEADER_OFFSET + 6
VOLUME_SELECTION_ALL = 0
FLAG_RECORD_SIZE = 6 * 3 * 2
FLAG_LINE_INDICES = bytes(
    (
        0, 2, 2, 1, 1, 3, 3, 0,
        0, 4, 1, 4, 2, 4, 3, 4,
        0, 5, 1, 5, 2, 5, 3, 5,
    )
)
FLAG_VERTICES_PER_MARKER = len(FLAG_LINE_INDICES)
FLAG_TRIGGER_COLOR = (255, 85, 215, 220)

T2 = 10
T3 = 11
T4 = 12
T5 = 13
S3 = 19

TERRAIN_KEYS = tuple(range(4, 10))
TERRAIN_LABELS = {
    4: "walkable",
    5: "steep",
    6: "ceilings",
    7: "query-0x200-excluded",
    8: "solid-one-sided",
    9: "solid-two-sided",
}
TERRAIN_COLORS = {
    4: (0, 255, 90, 78),
    5: (255, 150, 0, 88),
    6: (80, 120, 255, 82),
    7: (160, 160, 160, 72),
    8: (255, 0, 210, 82),
    9: (255, 255, 0, 92),
}
CLIMB_SURFACE_FLAG = 0x0080
MATERIAL_CLIMB_FENCE = 30
MATERIAL_CLIMB_WOOD = 178
CLIMB_CLASS_STANDARD = "standard"
CLIMB_CLASS_SPECIAL_WOOD = "special-wood"
CLIMB_COLORS = {
    CLIMB_CLASS_STANDARD: (20, 255, 90, 132),
    CLIMB_CLASS_SPECIAL_WOOD: (35, 235, 255, 142),
}
CLIMB_DYNAMIC_OBB_COLOR = (20, 255, 90, 255)
PLAYER_DEBUG_COLORS = {
    2: (255, 70, 70, 255),
    3: (40, 230, 255, 255),
    4: (255, 170, 45, 255),
}
OBJECT_OBB_COLOR = (255, 55, 230, 255)
OBB_LINE_INDICES = bytes(
    endpoint
    for corner in range(8)
    for axis_index in range(3)
    if not corner & (1 << axis_index)
    for endpoint in (corner, corner ^ (1 << axis_index))
)


def extend_data_with_flags(
    data_blob: bytes,
    document: dict,
    payload_end: int,
    scale: float,
) -> tuple[bytes, int, int, int, int]:
    """Append compact native octahedra for statically known trigger spheres."""
    blob = bytearray(data_blob)
    records = bytearray()
    trigger_flags = [
        flag for flag in document.get("flags", [])
        if flag.get("sphere_uses")
    ]
    for flag in trigger_flags:
        center = tuple(float(value) for value in flag["position"])
        radius = max(
            float(use["radius"]) for use in flag["sphere_uses"]
        )
        game_points = (
            (center[0] + radius, center[1], center[2]),
            (center[0] - radius, center[1], center[2]),
            (center[0], center[1] + radius, center[2]),
            (center[0], center[1] - radius, center[2]),
            (center[0], center[1], center[2] + radius),
            (center[0], center[1], center[2] - radius),
        )
        for x, y, z in game_points:
            for value in (x, z, -y):
                quantized = round(value / scale)
                if not -32768 <= quantized <= 32767:
                    raise ValueError("quantized flag point exceeds int16")
                records.extend(struct.pack("<h", quantized))

    if not trigger_flags:
        return (
            bytes(blob),
            payload_end,
            payload_end,
            payload_end,
            payload_end,
        )

    record_offset = map_trigger_probe.align_up(payload_end, 2)
    index_offset = record_offset + len(records)
    color_offset = index_offset + len(FLAG_LINE_INDICES)
    flag_payload_end = color_offset + 4
    if flag_payload_end > MARKER_OFFSET:
        raise ValueError(
            "compact native flag markers exceed the proven data block: "
            f"0x{flag_payload_end:X}"
        )
    blob[record_offset : record_offset + len(records)] = records
    blob[index_offset : index_offset + len(FLAG_LINE_INDICES)] = (
        FLAG_LINE_INDICES
    )
    struct.pack_into("<4B", blob, color_offset, *FLAG_TRIGGER_COLOR)
    return (
        bytes(blob),
        record_offset,
        index_offset,
        color_offset,
        flag_payload_end,
    )


def xor(rd: int, rs: int, rt: int) -> int:
    return (rs << 21) | (rt << 16) | (rd << 11) | 0x26


def terrain_categories(
    row: dict, wall_normal_z: float = 0.35
) -> set[int]:
    """Return every independent terrain toggle matching a collision face."""
    flags = int(row["flags"])
    if not collision_probe.blocks_standard_human(flags):
        return {7}

    categories = {9 if flags & 0x0002 else 8}
    normal_z = float(row["normal"][2])
    if normal_z >= collision_probe.WALKABLE_NORMAL_Z:
        categories.add(4)
    elif normal_z > wall_normal_z:
        categories.add(5)
    elif normal_z <= -wall_normal_z:
        categories.add(6)
    return categories


def terrain_display_category(
    row: dict,
    visibility: list[bool] | tuple[bool, ...],
    wall_normal_z: float = 0.35,
) -> int | None:
    """Choose a stable color among all currently enabled matching toggles."""
    enabled = {
        category
        for category in terrain_categories(row, wall_normal_z)
        if visibility[category]
    }
    if not enabled:
        return None
    # Temporary/two-sided barriers retain their semantic yellow.  Otherwise
    # orientation colors win over the broad common-solid category.
    for category in (7, 9, 4, 5, 6, 8):
        if category in enabled:
            return category
    return None


def classify_climb_surfaces(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int, int, int, int]],
    wall_normal_z: float = 0.35,
) -> dict[int, str]:
    """Classify the static surfaces used by the native climb-filter path.

    Live gameplay validation showed that solid near-vertical faces carrying
    collision bit 0x80 are the standard climb surfaces.  The native collision
    result path also accepts material 30 before testing that bit.  Material 178
    belongs to a separate CLIMB_WOOD path and remains a cyan diagnostic when
    such a face happens to be present in the static collision mesh.
    """
    classes: dict[int, str] = {}
    for index, (a, b, c, flags, material, tail) in enumerate(triangles):
        points = (vertices[a], vertices[b], vertices[c])
        flags = int(flags)
        if not collision_probe.blocks_standard_human(flags):
            continue
        if abs(float(collision_probe.normal(points)[2])) > wall_normal_z:
            continue
        if flags & CLIMB_SURFACE_FLAG:
            classes[index] = CLIMB_CLASS_STANDARD
        elif material == MATERIAL_CLIMB_FENCE:
            classes[index] = CLIMB_CLASS_STANDARD
        elif material == MATERIAL_CLIMB_WOOD:
            classes[index] = CLIMB_CLASS_SPECIAL_WOOD
    return classes


def select_terrain_rows(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int, int, int, int]],
    position: tuple[float, float, float],
    radius: float,
    limit: int,
    visibility: list[bool] | tuple[bool, ...],
    wall_normal_z: float,
    *,
    spatial_index: collision_probe.CollisionSpatialIndex | None = None,
    climb_classes: dict[int, str] | None = None,
) -> list[dict]:
    if climb_classes is not None:
        if spatial_index is not None:
            return spatial_index.select(
                position,
                radius,
                min(limit, TERRAIN_CAPACITY),
                lambda row: int(row["index"]) in climb_classes,
            )
        rows = collision_probe.select_triangles(
            vertices,
            triangles,
            position,
            radius,
            0,
            "all",
            wall_normal_z,
        )
        return [
            row for row in rows if int(row["index"]) in climb_classes
        ][: min(limit, TERRAIN_CAPACITY)]
    if spatial_index is not None:
        return spatial_index.select(
            position,
            radius,
            min(limit, TERRAIN_CAPACITY),
            lambda row: terrain_display_category(
                row, visibility, wall_normal_z
            )
            is not None,
        )
    rows = collision_probe.select_triangles(
        vertices,
        triangles,
        position,
        radius,
        0,
        "all",
        wall_normal_z,
    )
    selected = [
        row
        for row in rows
        if terrain_display_category(row, visibility, wall_normal_z) is not None
    ]
    return selected[: min(limit, TERRAIN_CAPACITY)]


def collision_vertex_pointer(client: PineClient, collision: int) -> int:
    pointer = client.read(collision + 0x8C, 32)
    if not 0x00700000 <= pointer < 0x02000000:
        raise RuntimeError(
            f"implausible collision vertex pointer: 0x{pointer:08X}"
        )
    return pointer


def build_terrain_records(
    rows: list[dict],
    triangles: list[tuple[int, int, int, int, int, int]],
    vertex_pointer: int,
    visibility: list[bool] | tuple[bool, ...],
    wall_normal_z: float,
    *,
    climb_classes: dict[int, str] | None = None,
) -> bytes:
    if len(rows) > TERRAIN_CAPACITY:
        raise ValueError(
            f"{len(rows)} terrain faces exceed capacity {TERRAIN_CAPACITY}"
        )
    records = bytearray()
    for row in rows:
        triangle_index = int(row["index"])
        a, b, c, _flags, _material, _tail = triangles[triangle_index]
        if climb_classes is not None:
            climb_class = climb_classes.get(triangle_index)
            if climb_class is None:
                raise ValueError("terrain row has no climb diagnostic class")
            color = CLIMB_COLORS[climb_class]
        else:
            category = terrain_display_category(
                row, visibility, wall_normal_z
            )
            if category is None:
                raise ValueError("terrain row does not match an enabled category")
            color = TERRAIN_COLORS[category]
        records.extend(
            struct.pack(
                "<3I4B",
                vertex_pointer + a * 16,
                vertex_pointer + b * 16,
                vertex_pointer + c * 16,
                *color,
            )
        )
    return bytes(records)


def update_terrain_records(
    client: PineClient,
    code_address: int,
    records: bytes,
    *,
    enabled: bool = True,
    record_address: int | None = None,
) -> int:
    if len(records) % TERRAIN_RECORD_SIZE:
        raise ValueError("terrain records have an invalid byte length")
    count = len(records) // TERRAIN_RECORD_SIZE
    if count > TERRAIN_CAPACITY:
        raise ValueError("too many terrain records")
    count_address = (
        code_address + STATUS_OFFSET + TERRAIN_COUNT_STATUS_OFFSET
    )
    # Disable first so the EE pass never observes a partially replaced list.
    client.write(count_address, 0, 32)
    if records:
        client.write_block(
            record_address
            if record_address is not None
            else code_address + TERRAIN_RECORD_OFFSET,
            records,
        )
    if enabled and count:
        client.write(count_address, count, 32)
    return count


def _normalized(
    vector: tuple[float, float, float]
) -> tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1.0e-8:
        return (0.0, 0.0, 1.0)
    return tuple(component / length for component in vector)


def _cross(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def build_player_debug_data(
    shapes: list,
) -> tuple[bytes, bytes, int]:
    """Build one batched native 3D line list from the sampled player bounds."""
    points: list[tuple[float, float, float]] = []
    lines: list[tuple[int, int, tuple[int, int, int, int]]] = []

    def add_point(point: tuple[float, float, float]) -> int:
        if len(points) >= PLAYER_DEBUG_MAX_POINTS:
            raise ValueError("player debug point capacity exceeded")
        points.append(point)
        return len(points) - 1

    def add_line(
        first: int,
        second: int,
        color: tuple[int, int, int, int],
    ) -> None:
        if len(lines) >= PLAYER_DEBUG_MAX_LINES:
            raise ValueError("player debug line capacity exceeded")
        lines.append((first, second, color))

    principal = next(
        (shape for shape in shapes if int(shape.kind) == 3), None
    )
    if principal is not None:
        first = tuple(principal.first)
        second = tuple(principal.second)
        radius = float(principal.radius)
        direction = _normalized(
            tuple(second[index] - first[index] for index in range(3))
        )
        reference = (
            (0.0, 0.0, 1.0)
            if abs(direction[2]) < 0.9
            else (1.0, 0.0, 0.0)
        )
        side = _normalized(_cross(direction, reference))
        other = _normalized(_cross(direction, side))
        bottom: list[int] = []
        top: list[int] = []
        for axis_a, axis_b in (
            (1.0, 0.0),
            (0.0, 1.0),
            (-1.0, 0.0),
            (0.0, -1.0),
        ):
            offset = tuple(
                (side[index] * axis_a + other[index] * axis_b) * radius
                for index in range(3)
            )
            bottom.append(
                add_point(
                    tuple(first[index] + offset[index] for index in range(3))
                )
            )
            top.append(
                add_point(
                    tuple(second[index] + offset[index] for index in range(3))
                )
            )
        color = PLAYER_DEBUG_COLORS[3]
        for index in range(4):
            following = (index + 1) % 4
            add_line(bottom[index], bottom[following], color)
            add_line(top[index], top[following], color)
            add_line(bottom[index], top[index], color)

    capsule = next(
        (shape for shape in shapes if int(shape.kind) == 4), None
    )
    if capsule is not None:
        first_position = tuple(capsule.first)
        second_position = tuple(capsule.second)
        first = add_point(first_position)
        second = add_point(second_position)
        color = PLAYER_DEBUG_COLORS[4]
        add_line(first, second, color)
        direction = _normalized(
            tuple(
                second_position[index] - first_position[index]
                for index in range(3)
            )
        )
        reference = (
            (0.0, 0.0, 1.0)
            if abs(direction[2]) < 0.9
            else (1.0, 0.0, 0.0)
        )
        side = _normalized(_cross(direction, reference))
        other = _normalized(_cross(direction, side))
        midpoint = tuple(
            (first_position[index] + second_position[index]) * 0.5
            for index in range(3)
        )
        for axis in (side, other):
            negative = tuple(
                midpoint[index] - axis[index] * float(capsule.radius)
                for index in range(3)
            )
            positive = tuple(
                midpoint[index] + axis[index] * float(capsule.radius)
                for index in range(3)
            )
            add_line(add_point(negative), add_point(positive), color)

    sphere = next(
        (shape for shape in shapes if int(shape.kind) == 2), None
    )
    if sphere is not None:
        center = tuple(sphere.first)
        radius = float(sphere.radius)
        for axis in range(3):
            negative = list(center)
            positive = list(center)
            negative[axis] -= radius
            positive[axis] += radius
            add_line(
                add_point(tuple(negative)),
                add_point(tuple(positive)),
                PLAYER_DEBUG_COLORS[2],
            )

    if len(points) > PLAYER_DEBUG_MAX_POINTS:
        raise ValueError("player debug point capacity exceeded")
    if len(lines) > PLAYER_DEBUG_MAX_LINES:
        raise ValueError("player debug line capacity exceeded")
    point_blob = bytearray()
    for point in points:
        x, y, z = point
        point_blob.extend(struct.pack("<3f", x, z, -y))
    line_blob = bytearray()
    for first, second, color in lines:
        line_blob.extend(
            struct.pack("<2B2x4B", first, second, *color)
        )
    return bytes(point_blob), bytes(line_blob), len(lines)


def _obb_corners(shape) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    center = tuple(float(value) for value in shape.center)
    half_extents = tuple(float(value) for value in shape.half_extents)
    axes = tuple(tuple(float(value) for value in axis) for axis in shape.axes)
    for corner in range(8):
        point = list(center)
        for axis_index, (axis, half_extent) in enumerate(
            zip(axes, half_extents)
        ):
            sign = 1.0 if corner & (1 << axis_index) else -1.0
            for component in range(3):
                point[component] += axis[component] * half_extent * sign
        points.append(tuple(point))
    return points


def build_obb_debug_data(shape) -> tuple[bytes, bytes, int]:
    """Build the legacy one-OBB line batch used by focused tests/tools."""
    points = _obb_corners(shape)

    edges = list(zip(OBB_LINE_INDICES[::2], OBB_LINE_INDICES[1::2]))
    if len(points) > PLAYER_DEBUG_MAX_POINTS:
        raise ValueError("object OBB point capacity exceeded")
    if len(edges) > PLAYER_DEBUG_MAX_LINES:
        raise ValueError("object OBB line capacity exceeded")

    point_blob = bytearray()
    for x, y, z in points:
        point_blob.extend(struct.pack("<3f", x, z, -y))
    line_blob = bytearray()
    for first, second in edges:
        line_blob.extend(
            struct.pack("<2B2x4B", first, second, *OBJECT_OBB_COLOR)
        )
    return bytes(point_blob), bytes(line_blob), len(edges)


def build_obb_debug_records(shapes: list) -> tuple[bytes, int]:
    """Pack up to 24 quantized live OBB corner records."""
    if len(shapes) > OBB_CAPACITY:
        raise ValueError(f"object OBB capacity exceeded: {len(shapes)}")
    records = bytearray()
    for shape in shapes:
        for x, y, z in _obb_corners(shape):
            quantized = tuple(
                round(value / OBB_COORD_SCALE) for value in (x, z, -y)
            )
            if any(not -32768 <= value <= 32767 for value in quantized):
                raise ValueError("object OBB corner exceeds int16 range")
            records.extend(struct.pack("<3h", *quantized))
    return bytes(records), len(shapes)


def extend_data_for_object_obbs(data_blob: bytes) -> bytes:
    """Create the layout shared by confirmed-climb and raw-debug OBBs."""
    if len(data_blob) > BLOB_SIZE:
        raise ValueError("base simultaneous data exceeds its stable allocation")
    blob = bytearray(EXTENDED_DATA_SIZE)
    blob[: len(data_blob)] = data_blob
    blob[
        OBB_INDEX_OFFSET : OBB_INDEX_OFFSET + len(OBB_LINE_INDICES)
    ] = OBB_LINE_INDICES
    struct.pack_into("<4B", blob, OBB_COLOR_OFFSET, *OBJECT_OBB_COLOR)
    struct.pack_into("<f", blob, OBB_SCALE_OFFSET, OBB_COORD_SCALE)
    return bytes(blob)


def update_obb_debug_records(
    client: PineClient,
    code_address: int,
    data_address: int,
    records: bytes,
    count: int,
    *,
    enabled: bool,
    color: tuple[int, int, int, int] | None = None,
) -> int:
    if not 0 <= count <= OBB_CAPACITY:
        raise ValueError("invalid object OBB count")
    if len(records) != count * OBB_POINT_RECORD_SIZE:
        raise ValueError("object OBB records have an invalid byte length")
    count_address = code_address + STATUS_OFFSET + OBB_COUNT_STATUS_OFFSET
    # Publish the new count last so the EE never reads a partially updated box.
    client.write(count_address, 0, 32)
    if color is not None:
        if len(color) != 4 or any(
            not 0 <= component <= 255 for component in color
        ):
            raise ValueError("object OBB color must contain four bytes")
        client.write(
            data_address + OBB_COLOR_OFFSET,
            int.from_bytes(bytes(color), "little"),
            32,
        )
    if records:
        client.write_block(data_address + OBB_POINT_RECORD_OFFSET, records)
    visible_count = count if enabled else 0
    if visible_count:
        client.write(count_address, visible_count, 32)
    return visible_count


def update_player_debug_data(
    client: PineClient,
    code_address: int,
    point_blob: bytes,
    line_blob: bytes,
    line_count: int,
    *,
    enabled: bool,
) -> None:
    if len(point_blob) > PLAYER_DEBUG_MAX_POINTS * PLAYER_DEBUG_POINT_SIZE:
        raise ValueError("player debug point block is too large")
    if len(line_blob) > PLAYER_DEBUG_MAX_LINES * PLAYER_DEBUG_LINE_SIZE:
        raise ValueError("player debug line block is too large")
    if not 0 <= line_count <= PLAYER_DEBUG_MAX_LINES:
        raise ValueError("invalid player debug line count")
    header_address = code_address + PLAYER_DEBUG_HEADER_OFFSET
    client.write(header_address, 0, 32)
    if point_blob:
        client.write_block(
            code_address + PLAYER_DEBUG_POINT_OFFSET, point_blob
        )
    if line_blob:
        client.write_block(
            code_address + PLAYER_DEBUG_LINE_OFFSET, line_blob
        )
    if enabled and line_count:
        client.write(header_address, line_count, 32)


def build_render_code(
    code_address: int,
    data_address: int,
    index_address: int,
    kind_address: int,
    color_address: int,
    visibility_address: int,
    scale_address: int,
    volume_count: int,
    flag_record_address: int,
    flag_index_address: int,
    flag_color_address: int,
    flag_current_count_address: int,
    collision_xray: bool = False,
    object_obb_support: bool = False,
) -> bytes:
    status_address = code_address + STATUS_OFFSET
    matrix_address = code_address + MATRIX_OFFSET
    destination_address = code_address + DESTINATION_OFFSET
    terrain_address = (
        data_address + EXTENDED_TERRAIN_RECORD_OFFSET
        if object_obb_support
        else code_address + TERRAIN_RECORD_OFFSET
    )
    terrain_count_address = (
        status_address + TERRAIN_COUNT_STATUS_OFFSET
    )
    player_header_address = code_address + PLAYER_DEBUG_HEADER_OFFSET
    player_point_address = code_address + PLAYER_DEBUG_POINT_OFFSET
    player_line_address = code_address + PLAYER_DEBUG_LINE_OFFSET
    obb_record_address = data_address + OBB_POINT_RECORD_OFFSET
    obb_index_address = data_address + OBB_INDEX_OFFSET
    obb_color_address = data_address + OBB_COLOR_OFFSET
    obb_scale_address = data_address + OBB_SCALE_OFFSET
    obb_count_address = status_address + OBB_COUNT_STATUS_OFFSET
    words: list[int] = [
        addiu(REG["sp"], REG["sp"], -0x60),
        sw(REG["ra"], 0x50, REG["sp"]),
        sw(REG["s0"], 0x40, REG["sp"]),
        sw(REG["s1"], 0x44, REG["sp"]),
        sw(REG["s2"], 0x48, REG["sp"]),
        sw(S3, 0x4C, REG["sp"]),
    ]

    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 0, REG["t0"]))

    # Preserve the displaced call and its return value.
    words.append(jal(MAIN_VIEW_ORIGINAL_CALLEE))
    words.append(0)
    words.append(sw(REG["v0"], 0x34, REG["sp"]))

    if collision_xray:
        # The isolated experimental launcher shows complete collision through
        # render geometry and from either winding.  The normal launcher keeps
        # its accepted depth-tested state path byte-for-byte.
        for state, value in ((1, 0), (6, 0), (20, 1)):
            call_render_state(words, 0x10, state, value=value)
    else:
        for state, value in ((7, 1), (1, 0), (14, 0)):
            call_render_state(words, 0x10, state, value=value)
    load_address(words, REG["a0"], matrix_address)
    words.append(jal(RW_MATRIX_SET_IDENTITY))
    words.append(0)

    # Trigger boxes: expand each quantized eight-corner record into the shared
    # 36-vertex buffer and honor the independent 0-3 visibility slots.  The
    # selection byte is zero for the complete set or one plus the renderer-
    # order index for the single-trigger inspection mode.
    load_address(words, REG["s0"], data_address)
    words.append(addiu(REG["s1"], REG["zero"], volume_count))
    load_address(words, S3, index_address)
    trigger_outer = len(words)
    load_address(words, T2, kind_address)
    words.append(addiu(T3, REG["zero"], volume_count))
    words.append(trigger_probe.subu(T3, T3, REG["s1"]))
    words.append(trigger_probe.addu(T2, T2, T3))
    words.append(trigger_probe.lbu(T5, 0, T2))
    load_address(words, T2, visibility_address)
    words.append(trigger_probe.addu(T2, T2, T5))
    words.append(trigger_probe.lbu(T2, 0, T2))
    load_address(words, T4, code_address + VOLUME_SELECTION_OFFSET)
    words.append(trigger_probe.lbu(T4, 0, T4))
    selection_all = len(words)
    words.extend((0, 0))
    selection_hidden = len(words)
    words.extend((0, 0))
    trigger_hidden = len(words)
    words.extend((0, 0))
    words.append(trigger_probe.sll(T5, T5, 2))
    load_address(words, T2, color_address)
    words.append(trigger_probe.addu(T5, T5, T2))
    words.append(lw(T5, 0, T5))
    load_address(words, T2, scale_address)
    words.append(map_trigger_probe.lwc1(map_trigger_probe.F1, 0, T2))

    load_address(words, REG["s2"], destination_address)
    words.append(addiu(REG["t0"], S3, 0))
    words.append(
        addiu(REG["t1"], REG["zero"], VERTICES_PER_SOLID_BOX)
    )
    trigger_inner = len(words)
    words.extend(
        (
            trigger_probe.lbu(T2, 0, REG["t0"]),
            trigger_probe.sll(T3, T2, 2),
            trigger_probe.sll(T4, T2, 1),
            trigger_probe.addu(T3, T3, T4),
            trigger_probe.addu(T3, REG["s0"], T3),
        )
    )
    for source_offset, destination_offset in ((0, 0), (2, 4), (4, 8)):
        words.extend(
            (
                map_trigger_probe.lh(T4, source_offset, T3),
                map_trigger_probe.mtc1(T4, map_trigger_probe.F0),
                map_trigger_probe.cvt_s_w(
                    map_trigger_probe.F0, map_trigger_probe.F0
                ),
                map_trigger_probe.mul_s(
                    map_trigger_probe.F0,
                    map_trigger_probe.F0,
                    map_trigger_probe.F1,
                ),
                map_trigger_probe.swc1(
                    map_trigger_probe.F0,
                    destination_offset,
                    REG["s2"],
                ),
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
    trigger_inner_branch = len(words)
    words.append(
        bne(
            REG["t1"],
            REG["zero"],
            trigger_inner - (trigger_inner_branch + 1),
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
    trigger_failed = len(words)
    words.extend((0, 0))
    words.append(addiu(REG["a0"], REG["zero"], 3))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    words.append(jal(RW_IM3D_END))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 16, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 16, REG["t0"]))
    trigger_end = len(words)
    words[trigger_failed] = beq(
        REG["v0"], REG["zero"], trigger_end - (trigger_failed + 1)
    )
    words[trigger_hidden] = beq(
        T2, REG["zero"], trigger_end - (trigger_hidden + 1)
    )
    words[selection_all] = beq(
        T4,
        REG["zero"],
        trigger_hidden - (selection_all + 1),
    )
    words[selection_all + 1] = addiu(T3, T3, 1)
    words[selection_hidden] = bne(
        T4,
        T3,
        trigger_end - (selection_hidden + 1),
    )
    # A matching solo index forces the existing visibility test true.  The
    # branch delay slot also runs for mismatches, which immediately jump to
    # trigger_end, so no hidden box can leak through.
    words[selection_hidden + 1] = addiu(T2, REG["zero"], 1)
    words.append(
        addiu(REG["s0"], REG["s0"], TRIGGER_RECORD_SIZE)
    )
    words.append(addiu(REG["s1"], REG["s1"], -1))
    trigger_outer_branch = len(words)
    words.append(
        bne(
            REG["s1"],
            REG["zero"],
            trigger_outer - (trigger_outer_branch + 1),
        )
    )
    words.append(0)

    # Trigger flags: expand each compact six-point octahedron into a native
    # 24-vertex line list.  The live marker count selects visible or hidden.
    load_address(words, REG["t0"], flag_current_count_address)
    words.append(trigger_probe.lbu(REG["s1"], 0, REG["t0"]))
    flag_empty = len(words)
    words.extend((0, 0))
    load_address(words, REG["s0"], flag_record_address)
    load_address(words, S3, flag_index_address)
    load_address(words, T2, scale_address)
    words.append(map_trigger_probe.lwc1(map_trigger_probe.F1, 0, T2))
    load_address(words, T2, flag_color_address)
    words.append(lw(T5, 0, T2))

    flag_outer = len(words)
    load_address(words, REG["s2"], destination_address)
    words.append(addiu(REG["t0"], S3, 0))
    words.append(
        addiu(REG["t1"], REG["zero"], FLAG_VERTICES_PER_MARKER)
    )
    flag_inner = len(words)
    words.extend(
        (
            trigger_probe.lbu(T2, 0, REG["t0"]),
            trigger_probe.sll(T3, T2, 2),
            trigger_probe.sll(T4, T2, 1),
            trigger_probe.addu(T3, T3, T4),
            trigger_probe.addu(T3, REG["s0"], T3),
        )
    )
    for source_offset, destination_offset in ((0, 0), (2, 4), (4, 8)):
        words.extend(
            (
                map_trigger_probe.lh(T4, source_offset, T3),
                map_trigger_probe.mtc1(T4, map_trigger_probe.F0),
                map_trigger_probe.cvt_s_w(
                    map_trigger_probe.F0, map_trigger_probe.F0
                ),
                map_trigger_probe.mul_s(
                    map_trigger_probe.F0,
                    map_trigger_probe.F0,
                    map_trigger_probe.F1,
                ),
                map_trigger_probe.swc1(
                    map_trigger_probe.F0,
                    destination_offset,
                    REG["s2"],
                ),
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
    flag_inner_branch = len(words)
    words.append(
        bne(
            REG["t1"],
            REG["zero"],
            flag_inner - (flag_inner_branch + 1),
        )
    )
    words.append(0)

    load_address(words, REG["a0"], destination_address)
    words.append(
        addiu(REG["a1"], REG["zero"], FLAG_VERTICES_PER_MARKER)
    )
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    flag_failed = len(words)
    words.extend((0, 0))
    words.append(addiu(REG["a0"], REG["zero"], 1))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    words.append(jal(RW_IM3D_END))
    words.append(0)
    flag_marker_end = len(words)
    words.append(addiu(REG["s0"], REG["s0"], FLAG_RECORD_SIZE))
    words.append(addiu(REG["s1"], REG["s1"], -1))
    flag_outer_branch = len(words)
    words.append(
        bne(
            REG["s1"],
            REG["zero"],
            flag_outer - (flag_outer_branch + 1),
        )
    )
    words.append(0)
    flag_end = len(words)
    words[flag_failed] = beq(
        REG["v0"],
        REG["zero"],
        flag_marker_end - (flag_failed + 1),
    )
    words[flag_empty] = beq(
        REG["s1"], REG["zero"], flag_end - (flag_empty + 1)
    )

    # Terrain faces: each 16-byte record holds three pointers into the live
    # level vertex array plus one RGBA color.  Convert game XYZ to RenderWare
    # X,Z,-Y while copying three vertices into the shared buffer.
    load_address(words, REG["t0"], terrain_count_address)
    words.append(lw(REG["s1"], 0, REG["t0"]))
    terrain_empty = len(words)
    words.extend((0, 0))
    load_address(words, REG["s0"], terrain_address)
    terrain_outer = len(words)
    load_address(words, REG["s2"], destination_address)
    words.append(addiu(REG["t0"], REG["s0"], 0))
    words.append(addiu(REG["t1"], REG["zero"], VERTICES_PER_TRIANGLE))
    words.append(lw(T5, 12, REG["s0"]))
    words.append(lui(T4, 0x8000))
    terrain_inner = len(words)
    words.extend(
        (
            lw(T2, 0, REG["t0"]),
            lw(T3, 0, T2),
            sw(T3, 0, REG["s2"]),
            lw(T3, 8, T2),
            sw(T3, 4, REG["s2"]),
            lw(T3, 4, T2),
            xor(T3, T3, T4),
            sw(T3, 8, REG["s2"]),
            sw(T5, 12, REG["s2"]),
            addiu(REG["t0"], REG["t0"], 4),
            addiu(REG["s2"], REG["s2"], 36),
            addiu(REG["t1"], REG["t1"], -1),
        )
    )
    terrain_inner_branch = len(words)
    words.append(
        bne(
            REG["t1"],
            REG["zero"],
            terrain_inner - (terrain_inner_branch + 1),
        )
    )
    words.append(0)

    load_address(words, REG["a0"], destination_address)
    words.append(
        addiu(REG["a1"], REG["zero"], VERTICES_PER_TRIANGLE)
    )
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    terrain_failed = len(words)
    words.extend((0, 0))
    words.append(addiu(REG["a0"], REG["zero"], 3))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    words.append(jal(RW_IM3D_END))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 20, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 20, REG["t0"]))
    terrain_end = len(words)
    words[terrain_failed] = beq(
        REG["v0"], REG["zero"], terrain_end - (terrain_failed + 1)
    )
    words.append(
        addiu(REG["s0"], REG["s0"], TERRAIN_RECORD_SIZE)
    )
    words.append(addiu(REG["s1"], REG["s1"], -1))
    terrain_outer_branch = len(words)
    words.append(
        bne(
            REG["s1"],
            REG["zero"],
            terrain_outer - (terrain_outer_branch + 1),
        )
    )
    words.append(0)

    # Live-object OBBs: each host-updated record contains eight
    # quantized RenderWare-space int16 corners.  Expand the fixed 12 edges into one
    # 24-vertex line-list batch per box.  Keeping one box per transform bounds
    # the shared destination buffer while still allowing 24 nearby boxes.
    obb_empty = None
    if object_obb_support:
        obb_start = len(words)
        words[terrain_empty] = beq(
            REG["s1"],
            REG["zero"],
            obb_start - (terrain_empty + 1),
        )
        load_address(words, REG["t0"], obb_count_address)
        words.append(lw(REG["s1"], 0, REG["t0"]))
        obb_empty = len(words)
        words.extend((0, 0))
        load_address(words, REG["s0"], obb_record_address)
        load_address(words, S3, obb_index_address)
        obb_outer = len(words)
        load_address(words, REG["s2"], destination_address)
        words.append(addiu(REG["t0"], S3, 0))
        words.append(
            addiu(REG["t1"], REG["zero"], OBB_VERTICES_PER_BOX)
        )
        load_address(words, T2, obb_color_address)
        words.append(lw(T5, 0, T2))
        load_address(words, T2, obb_scale_address)
        words.append(map_trigger_probe.lwc1(map_trigger_probe.F1, 0, T2))
        obb_inner = len(words)
        words.extend(
            (
                trigger_probe.lbu(T2, 0, REG["t0"]),
                trigger_probe.sll(T3, T2, 2),
                trigger_probe.sll(T2, T2, 1),
                trigger_probe.addu(T2, T2, T3),
                trigger_probe.addu(T2, REG["s0"], T2),
            )
        )
        for source_offset, destination_offset in ((0, 0), (2, 4), (4, 8)):
            words.extend(
                (
                    map_trigger_probe.lh(T3, source_offset, T2),
                    map_trigger_probe.mtc1(T3, map_trigger_probe.F0),
                    map_trigger_probe.cvt_s_w(
                        map_trigger_probe.F0, map_trigger_probe.F0
                    ),
                    map_trigger_probe.mul_s(
                        map_trigger_probe.F0,
                        map_trigger_probe.F0,
                        map_trigger_probe.F1,
                    ),
                    map_trigger_probe.swc1(
                        map_trigger_probe.F0,
                        destination_offset,
                        REG["s2"],
                    ),
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
        obb_inner_branch = len(words)
        words.append(
            bne(
                REG["t1"],
                REG["zero"],
                obb_inner - (obb_inner_branch + 1),
            )
        )
        words.append(0)
        load_address(words, REG["a0"], destination_address)
        words.append(
            addiu(REG["a1"], REG["zero"], OBB_VERTICES_PER_BOX)
        )
        load_address(words, REG["a2"], matrix_address)
        words.append(addiu(REG["a3"], REG["zero"], 1))
        words.append(jal(RW_IM3D_TRANSFORM))
        words.append(0)
        obb_failed = len(words)
        words.extend((0, 0))
        words.append(addiu(REG["a0"], REG["zero"], 1))
        words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
        words.append(0)
        words.append(jal(RW_IM3D_END))
        words.append(0)
        obb_box_end = len(words)
        words[obb_failed] = beq(
            REG["v0"], REG["zero"], obb_box_end - (obb_failed + 1)
        )
        words.append(addiu(REG["s0"], REG["s0"], OBB_POINT_RECORD_SIZE))
        words.append(addiu(REG["s1"], REG["s1"], -1))
        obb_outer_branch = len(words)
        words.append(
            bne(
                REG["s1"],
                REG["zero"],
                obb_outer - (obb_outer_branch + 1),
            )
        )
        words.append(0)

    # Player bounds retain their independent host-updated line batch.  Disable
    # only the Z test for this batch so T can coexist with the OBB pass.
    player_start = len(words)
    if object_obb_support:
        assert obb_empty is not None
        words[obb_empty] = beq(
            REG["s1"],
            REG["zero"],
            player_start - (obb_empty + 1),
        )
    else:
        words[terrain_empty] = beq(
            REG["s1"],
            REG["zero"],
            player_start - (terrain_empty + 1),
        )
    load_address(words, REG["t0"], player_header_address)
    words.append(lw(REG["s1"], 0, REG["t0"]))
    player_empty = len(words)
    words.extend((0, 0))
    call_render_state(words, 0x10, 6, value=0)
    words.append(trigger_probe.sll(T2, REG["s1"], 1))
    words.append(sw(T2, 0x30, REG["sp"]))
    load_address(words, REG["s0"], player_line_address)
    load_address(words, S3, player_point_address)
    load_address(words, REG["s2"], destination_address)
    player_outer = len(words)
    words.append(lw(T5, 4, REG["s0"]))
    words.extend(
        (
            trigger_probe.lbu(T2, 0, REG["s0"]),
            trigger_probe.sll(T3, T2, 3),
            trigger_probe.sll(T2, T2, 2),
            trigger_probe.addu(T2, T2, T3),
            trigger_probe.addu(T2, S3, T2),
            lw(T3, 0, T2),
            sw(T3, 0, REG["s2"]),
            lw(T3, 4, T2),
            sw(T3, 4, REG["s2"]),
            lw(T3, 8, T2),
            sw(T3, 8, REG["s2"]),
            sw(T5, 12, REG["s2"]),
            addiu(REG["s2"], REG["s2"], 36),
            trigger_probe.lbu(T2, 1, REG["s0"]),
            trigger_probe.sll(T3, T2, 3),
            trigger_probe.sll(T2, T2, 2),
            trigger_probe.addu(T2, T2, T3),
            trigger_probe.addu(T2, S3, T2),
            lw(T3, 0, T2),
            sw(T3, 0, REG["s2"]),
            lw(T3, 4, T2),
            sw(T3, 4, REG["s2"]),
            lw(T3, 8, T2),
            sw(T3, 8, REG["s2"]),
            sw(T5, 12, REG["s2"]),
            addiu(REG["s2"], REG["s2"], 36),
            addiu(REG["s0"], REG["s0"], PLAYER_DEBUG_LINE_SIZE),
            addiu(REG["s1"], REG["s1"], -1),
        )
    )
    player_outer_branch = len(words)
    words.append(
        bne(
            REG["s1"],
            REG["zero"],
            player_outer - (player_outer_branch + 1),
        )
    )
    words.append(0)
    load_address(words, REG["a0"], destination_address)
    words.append(lw(REG["a1"], 0x30, REG["sp"]))
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    player_failed = len(words)
    words.extend((0, 0))
    words.append(addiu(REG["a0"], REG["zero"], 1))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    words.append(jal(RW_IM3D_END))
    words.append(0)
    player_end = len(words)
    words[player_failed] = beq(
        REG["v0"], REG["zero"], player_end - (player_failed + 1)
    )
    call_render_state(words, 0x10, 6, value=1)

    restore = len(words)
    words[player_empty] = beq(
        REG["s1"], REG["zero"], restore - (player_empty + 1)
    )
    if collision_xray:
        for state, value in ((20, 2), (6, 1)):
            call_render_state(words, 0x10, state, value=value)
    else:
        for state, value in ((7, 2), (1, 0)):
            call_render_state(words, 0x10, state, value=value)
    words.extend(
        (
            lw(REG["v0"], 0x34, REG["sp"]),
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
    color_address: int,
    visibility_address: int,
    scale_address: int,
    volume_count: int,
    flag_record_address: int,
    flag_index_address: int,
    flag_color_address: int,
    flag_current_count_address: int,
    flag_count: int,
    collision_xray: bool = False,
    object_obb_support: bool = False,
) -> tuple[bytes, int]:
    code = build_render_code(
        code_address,
        data_address,
        index_address,
        kind_address,
        color_address,
        visibility_address,
        scale_address,
        volume_count,
        flag_record_address,
        flag_index_address,
        flag_color_address,
        flag_current_count_address,
        collision_xray,
        object_obb_support,
    )
    code_limit = min(
        DESTINATION_OFFSET if object_obb_support else TERRAIN_RECORD_OFFSET,
        AIR_STAND_STUB_OFFSETS[0],
    )
    if len(code) > code_limit:
        raise ValueError(
            f"simultaneous render code is too large: 0x{len(code):X}"
        )
    if (
        DESTINATION_OFFSET + VERTICES_PER_SOLID_BOX * 36
        > STATUS_OFFSET
    ):
        raise ValueError("shared vertex buffer overlaps status data")
    if (
        PLAYER_DEBUG_LINE_OFFSET
        + PLAYER_DEBUG_MAX_LINES * PLAYER_DEBUG_LINE_SIZE
        > CAMERA_OVERRIDE_STUB_OFFSET
    ):
        raise ValueError("player debug data overlap camera-override stub")
    if PLAYER_DEBUG_POINT_OFFSET < (
        PLAYER_DEBUG_HEADER_OFFSET + 16
    ):
        raise ValueError("player debug header overlaps point data")
    if PLAYER_DEBUG_LINE_OFFSET < (
        PLAYER_DEBUG_POINT_OFFSET
        + PLAYER_DEBUG_MAX_POINTS * PLAYER_DEBUG_POINT_SIZE
    ):
        raise ValueError("player debug points overlap line data")
    if object_obb_support:
        terrain_end = (
            EXTENDED_TERRAIN_RECORD_OFFSET
            + TERRAIN_CAPACITY * TERRAIN_RECORD_SIZE
        )
        obb_record_end = (
            OBB_POINT_RECORD_OFFSET
            + OBB_CAPACITY * OBB_POINT_RECORD_SIZE
        )
        if terrain_end > OBB_POINT_RECORD_OFFSET:
            raise ValueError("extended terrain records overlap object OBB records")
        if obb_record_end > OBB_INDEX_OFFSET:
            raise ValueError("object OBB records overlap fixed line indices")
        if OBB_INDEX_OFFSET + len(OBB_LINE_INDICES) > OBB_COLOR_OFFSET:
            raise ValueError("object OBB indices overlap their color")
        if OBB_COLOR_OFFSET + 4 > EXTENDED_DATA_SIZE:
            raise ValueError("object OBB color exceeds extended data allocation")
        if OBB_SCALE_OFFSET + 4 > EXTENDED_DATA_SIZE:
            raise ValueError("object OBB scale exceeds extended data allocation")
        if OBB_VERTICES_PER_BOX * 36 > (
            PLAYER_DEBUG_HEADER_OFFSET - DESTINATION_OFFSET
        ):
            raise ValueError("object OBB vertex batch exceeds shared buffer")
    camera_override_stub = map_trigger_probe.build_camera_override_stub(
        code_address
    )
    if (
        CAMERA_OVERRIDE_STUB_OFFSET + len(camera_override_stub)
        > CAMERA_OVERRIDE_MATRIX_OFFSET
    ):
        raise ValueError("camera-override stub overlaps its matrix")
    if CAMERA_OVERRIDE_MATRIX_OFFSET + 0x40 > CAMERA_OVERRIDE_FLAG_OFFSET:
        raise ValueError("camera-override matrix overlaps its state")
    if CAMERA_OVERRIDE_FLAG_OFFSET + 8 > MATRIX_OFFSET:
        raise ValueError("camera-override state overlaps renderer identity matrix")
    if MATRIX_OFFSET + 0x40 > STATUS_OFFSET:
        raise ValueError("renderer identity matrix overlaps status")
    if not 0 <= flag_count <= 255:
        raise ValueError("native flag count exceeds compact header")
    if (
        FLAG_RENDER_HEADER_OFFSET < STATUS_OFFSET + 0x1C
        or FLAG_RENDER_HEADER_OFFSET + FLAG_RENDER_HEADER.size > MARKER_OFFSET
    ):
        raise ValueError("native flag header overlaps renderer state")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    write_air_stand_layout(blob, code_address)
    if AIR_STAND_END_OFFSET > DESTINATION_OFFSET:
        raise ValueError("air-stand layout overlaps the shared vertex buffer")
    blob[
        CAMERA_OVERRIDE_STUB_OFFSET
        : CAMERA_OVERRIDE_STUB_OFFSET + len(camera_override_stub)
    ] = camera_override_stub
    FLAG_RENDER_HEADER.pack_into(
        blob,
        FLAG_RENDER_HEADER_OFFSET,
        FLAG_RENDER_MAGIC,
        flag_count,
        flag_count,
        VOLUME_SELECTION_ALL,
    )
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    if object_obb_support:
        blob = native_freecam.extend_blob(bytes(blob), code_address)
    return bytes(blob), len(code)


def disarm(client: PineClient, code_address: int) -> None:
    """Restore renderer hooks without invoking the game heap allocator."""
    import controller_input
    from map_visibility_control import disarm_range
    disarm_range(client, code_address)
    controller_input.disarm(client, code_address)
    native_freecam.disarm(client, code_address)
    installed_hook = jal(code_address)
    current_hook = client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
    if current_hook not in (MAIN_VIEW_HOOK_ORIGINAL, installed_hook):
        raise RuntimeError(
            f"unexpected simultaneous main-view hook: 0x{current_hook:08X}"
        )
    installed_camera_hook = jal(
        code_address + CAMERA_OVERRIDE_STUB_OFFSET
    )
    current_camera_hook = client.read(
        map_trigger_probe.CAMERA_BEGIN_UPDATE_HOOK,
        32,
    )
    if current_camera_hook not in (
        map_trigger_probe.CAMERA_BEGIN_UPDATE_ORIGINAL,
        installed_camera_hook,
    ):
        raise RuntimeError(
            "unexpected simultaneous camera BeginUpdate hook: "
            f"0x{current_camera_hook:08X}"
        )
    client.write(
        map_trigger_probe.CAMERA_BEGIN_UPDATE_HOOK,
        map_trigger_probe.CAMERA_BEGIN_UPDATE_ORIGINAL,
        32,
    )
    client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
    if (
        client.read(map_trigger_probe.CAMERA_BEGIN_UPDATE_HOOK, 32)
        != map_trigger_probe.CAMERA_BEGIN_UPDATE_ORIGINAL
        or client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
        != MAIN_VIEW_HOOK_ORIGINAL
    ):
        raise RuntimeError("simultaneous renderer hook restoration did not verify")


def cleanup(
    client: PineClient,
    code_address: int,
    data_address: int,
    *,
    if_owned: bool = False,
) -> bool:
    """Disarm and release owned blocks; return whether they were released."""
    disarm(client, code_address)
    code_marker = client.read_block(
        code_address + MARKER_OFFSET, 16, progress=False
    )
    data_marker = client.read_block(
        data_address + MARKER_OFFSET, 16, progress=False
    )
    if if_owned and (
        code_marker != MARKER or data_marker != DATA_MARKER
    ):
        print(
            "simultaneous renderer blocks were reclaimed or overwritten; "
            "no heap free was attempted",
            flush=True,
        )
        return False
    if code_marker != MARKER:
        raise RuntimeError("simultaneous renderer code marker mismatch")
    if data_marker != DATA_MARKER:
        raise RuntimeError("simultaneous renderer data marker mismatch")
    # Retire any call which entered before hooks were restored, including the
    # range entry. A completed native pass is required before releasing code.
    try:
        ready = probe_heap_callsite(client, timeout=3.0)
    except BootstrapUncertain as error:
        raise BootstrapCancelled(f"Native retirement unconfirmed; no heap release attempted: {error}") from error
    if not ready:
        raise BootstrapNotEntered('Renderer retirement callsite inactive; heap blocks retained')
    disarm(client, code_address)
    if (client.read_block(code_address+MARKER_OFFSET, 16, progress=False) != MARKER
            or client.read_block(data_address+MARKER_OFFSET, 16, progress=False) != DATA_MARKER):
        raise RuntimeError('Renderer ownership changed during retirement; heap blocks retained')
    try:
        run_heap_free_pair_bootstrap(client, data_address, code_address)
    except BootstrapNotEntered:
        raise
    except (ConnectionError, OSError):
        raise
    except Exception as error:
        raise BootstrapUncertain(f"Heap release did not confirm completion: {error}") from error
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "volumes",
        type=Path,
        nargs="?",
        default=Path("research/all_city_volumes.json"),
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--collision-radius", type=float, default=35.0)
    parser.add_argument(
        "--collision-limit",
        type=int,
        default=TERRAIN_CAPACITY,
    )
    parser.add_argument("--wall-normal-z", type=float, default=0.35)
    parser.add_argument(
        "--collision-xray",
        action="store_true",
        help="disable depth rejection for the experimental trigger/terrain pass",
    )
    parser.add_argument(
        "--object-obb-support",
        action="store_true",
        help="reserve and render the nearby live-object OBB batch",
    )
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument(
        "--start-world-time-scale",
        type=float,
        help="slow native world time immediately after hook installation",
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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.collision_radius < 0:
        raise ValueError("collision radius cannot be negative")
    if not 1 <= args.collision_limit <= TERRAIN_CAPACITY:
        raise ValueError(
            f"collision limit must be between 1 and {TERRAIN_CAPACITY}"
        )
    if not 0 <= args.wall_normal_z <= 1:
        raise ValueError("wall-normal-z must be between zero and one")
    if args.start_world_time_scale is not None and (
        not math.isfinite(args.start_world_time_scale)
        or not 0.0 <= args.start_world_time_scale < 1.0
    ):
        raise ValueError("start world time scale must be finite and in [0, 1)")
    if args.start_world_time_scale is not None and not args.install_only:
        raise ValueError("start world time scale requires --install-only")
    cleanup_requested = (
        args.cleanup_code_pointer is not None
        or args.cleanup_data_pointer is not None
    )
    if cleanup_requested and not args.disarm_only and (
        args.cleanup_code_pointer is None
        or args.cleanup_data_pointer is None
    ):
        raise ValueError("cleanup requires both code and data pointers")
    if cleanup_requested and (args.install_only or args.dry_run):
        raise ValueError("cleanup is mutually exclusive with install/dry-run")
    if args.cleanup_if_owned and not cleanup_requested:
        raise ValueError("cleanup-if-owned requires both cleanup pointers")
    if args.disarm_only and (
        args.cleanup_code_pointer is None
        or args.cleanup_data_pointer is not None
        or args.install_only
        or args.dry_run
    ):
        raise ValueError(
            "disarm-only requires only --cleanup-code-pointer"
        )

    document = json.loads(args.volumes.read_text(encoding="utf-8"))
    client: PineClient | None = PineClient(port=args.port)
    allocations: list[int] = []
    hook_installed = False
    world_time: WorldTimeController | None = None
    world_time_handed_off = False
    try:
        assert client is not None
        if client.text(GAME_ID) != "SLUS-21215":
            raise RuntimeError("The Warriors SLUS-21215 is not running")
        if args.disarm_only:
            assert args.cleanup_code_pointer is not None
            disarm(client, args.cleanup_code_pointer)
            print(
                "disarmed simultaneous renderer "
                f"code=0x{args.cleanup_code_pointer:08X}",
                flush=True,
            )
            return
        if cleanup_requested:
            assert args.cleanup_code_pointer is not None
            assert args.cleanup_data_pointer is not None
            released = cleanup(
                client,
                args.cleanup_code_pointer,
                args.cleanup_data_pointer,
                if_owned=args.cleanup_if_owned,
            )
            cleanup_label = (
                "cleaned simultaneous renderer "
                if released
                else "abandoned reclaimed simultaneous renderer "
            )
            print(
                cleanup_label
                +
                f"code=0x{args.cleanup_code_pointer:08X} "
                f"data=0x{args.cleanup_data_pointer:08X}"
            )
            return

        trigger_probe.validate_clean_state(client)
        transform = resolve_camera_transform(client)
        camera_position = struct.unpack_from(
            "<3f",
            client.read_block(transform, 16, progress=False),
        )
        trigger_rows = trigger_probe.select_volumes(
            document, camera_position, 0.0, 0
        )
        trigger_volumes = [
            volume for _distance, volume in trigger_rows
        ]
        (
            base_data_blob,
            index_offset,
            kind_offset,
            color_offset,
            visibility_offset,
            scale_offset,
            payload_end,
            max_error,
        ) = map_trigger_probe.build_quantized_data(trigger_volumes)
        scale = struct.unpack_from("<f", base_data_blob, scale_offset)[0]
        (
            data_blob,
            flag_record_offset,
            flag_index_offset,
            flag_color_offset,
            flag_payload_end,
        ) = extend_data_with_flags(
            base_data_blob,
            document,
            payload_end,
            scale,
        )
        if args.object_obb_support:
            data_blob = extend_data_for_object_obbs(data_blob)
        trigger_flag_count = sum(
            1
            for flag in document.get("flags", [])
            if flag.get("sphere_uses")
        )
        collision, vertices, triangles = collision_probe.read_collision(
            client
        )
        vertex_pointer = collision_vertex_pointer(client, collision)
        visibility = [True] * KIND_SLOT_COUNT
        terrain_rows = select_terrain_rows(
            vertices,
            triangles,
            camera_position,
            args.collision_radius,
            args.collision_limit,
            visibility,
            args.wall_normal_z,
        )
        terrain_records = build_terrain_records(
            terrain_rows,
            triangles,
            vertex_pointer,
            visibility,
            args.wall_normal_z,
        )
        _preview, code_size = build_code_block(
            0x00800000,
            0x00900000,
            0x00900000 + index_offset,
            0x00900000 + kind_offset,
            0x00900000 + color_offset,
            0x00900000 + visibility_offset,
            0x00900000 + scale_offset,
            len(trigger_volumes),
            0x00900000 + flag_record_offset,
            0x00900000 + flag_index_offset,
            0x00900000 + flag_color_offset,
            0x00800000 + FLAG_RENDER_HEADER_OFFSET + 5,
            trigger_flag_count,
            collision_xray=args.collision_xray,
            object_obb_support=args.object_obb_support,
        )
        print(
            f"game={client.text(TITLE)} triggers={len(trigger_volumes)} "
            f"flags={document.get('trigger_flag_count', 0)}"
            f"/{len(document.get('flags', []))} "
            f"terrain={len(terrain_rows)}/{TERRAIN_CAPACITY} "
            f"code=0x{code_size:X} records=0x{len(terrain_records):X} "
            f"trigger-payload=0x{payload_end:X} "
            f"flag-payload=0x{flag_payload_end:X} "
            f"data=0x{len(data_blob):X} "
            f"obbs={OBB_CAPACITY if args.object_obb_support else 0} "
            f"max-error={max_error:.5f}"
        )
        if args.dry_run:
            print("dry-run: simultaneous layout validated; no memory changed")
            return

        if args.start_world_time_scale is not None:
            waiting_notice_printed = False
            while True:
                emulator_status = client.status()
                if emulator_status == 0:
                    break
                if emulator_status == 2:
                    raise RuntimeError(
                        "PCSX2 shut down while waiting to install"
                    )
                if not waiting_notice_printed:
                    print(
                        "WAITING_FOR_PCSX2_RESUME=1 "
                        "(no EE memory has been changed)",
                        flush=True,
                    )
                    waiting_notice_printed = True
                time.sleep(0.05)

        code_address = run_heap_bootstrap(client, BLOB_SIZE)
        allocations.append(code_address)
        data_address = run_heap_bootstrap(client, len(data_blob))
        if data_address in allocations:
            raise RuntimeError(
                f"duplicate EE heap pointer: 0x{data_address:08X}"
            )
        allocations.append(data_address)
        if not (
            0x00700000 <= code_address < 0x01FF0000
            and 0x00700000 <= data_address
            and data_address + len(data_blob) < 0x01FFF000
        ):
            raise RuntimeError("EE heap pointer outside expected range")

        client.write_block(data_address, data_blob)
        code_blob, _ = build_code_block(
            code_address,
            data_address,
            data_address + index_offset,
            data_address + kind_offset,
            data_address + color_offset,
            data_address + visibility_offset,
            data_address + scale_offset,
            len(trigger_volumes),
            data_address + flag_record_offset,
            data_address + flag_index_offset,
            data_address + flag_color_offset,
            code_address + FLAG_RENDER_HEADER_OFFSET + 5,
            trigger_flag_count,
            collision_xray=args.collision_xray,
            object_obb_support=args.object_obb_support,
        )
        client.write_block(code_address, code_blob)
        update_terrain_records(
            client,
            code_address,
            terrain_records,
            enabled=True,
            record_address=(
                data_address + EXTENDED_TERRAIN_RECORD_OFFSET
                if args.object_obb_support
                else None
            ),
        )
        client.write(MAIN_VIEW_HOOK_ADDRESS, jal(code_address), 32)
        hook_installed = True
        if client.read(MAIN_VIEW_HOOK_ADDRESS, 32) != jal(code_address):
            raise RuntimeError("simultaneous hook installation did not verify")
        if args.start_world_time_scale is not None:
            world_time = WorldTimeController(
                client,
                args.start_world_time_scale,
            )
            original_time, target_time = world_time.enable()
            assert world_time.original_bits is not None
            assert world_time.target_bits is not None
            print(
                "WORLD_TIME_ORIGINAL_BITS="
                f"0x{world_time.original_bits:08X}",
                flush=True,
            )
            print(
                "WORLD_TIME_TARGET_BITS="
                f"0x{world_time.target_bits:08X}",
                flush=True,
            )
            print(
                f"WORLD_TIME_VALUES={original_time:.9g}->{target_time:.9g}",
                flush=True,
            )
        print(
            f"simultaneous render active code=0x{code_address:08X} "
            f"data=0x{data_address:08X}",
            flush=True,
        )
        if args.install_only:
            print(
                f"ACTIVE_CODE_POINTER=0x{code_address:08X}",
                flush=True,
            )
            print(
                f"ACTIVE_DATA_POINTER=0x{data_address:08X}",
                flush=True,
            )
            print(
                "ACTIVE_KIND_VISIBILITY_POINTER="
                f"0x{data_address + visibility_offset:08X}",
                flush=True,
            )
            print(
                "ACTIVE_TERRAIN_COUNT_POINTER="
                f"0x{code_address + STATUS_OFFSET + TERRAIN_COUNT_STATUS_OFFSET:08X}",
                flush=True,
            )
            print(
                "ACTIVE_FLAG_RENDER_STATE_POINTER="
                f"0x{code_address + FLAG_RENDER_HEADER_OFFSET:08X}",
                flush=True,
            )
            print(
                "ACTIVE_VOLUME_SELECTION_POINTER="
                f"0x{code_address + VOLUME_SELECTION_OFFSET:08X}",
                flush=True,
            )
            print(
                f"ACTIVE_VOLUME_COUNT={len(trigger_volumes)}",
                flush=True,
            )
            source_indices = {
                id(volume): index
                for index, volume in enumerate(document["volumes"])
            }
            print(
                "ACTIVE_VOLUME_ORDER="
                + ",".join(
                    str(source_indices[id(volume)])
                    for volume in trigger_volumes
                ),
                flush=True,
            )
            print(
                f"ACTIVE_FLAG_TRIGGER_COUNT={trigger_flag_count}",
                flush=True,
            )
            print(f"ACTIVE_TERRAIN_CAPACITY={TERRAIN_CAPACITY}", flush=True)
            world_time_handed_off = world_time is not None
            hook_installed = False
            allocations.clear()
            return
    finally:
        if (
            world_time is not None
            and world_time.active
            and not world_time_handed_off
        ):
            try:
                world_time.disable()
            except Exception as error:
                print(
                    f"WARNING: could not restore world timestep: {error}"
                )
        cleanup_safe = client is not None
        if hook_installed and client is not None:
            try:
                client.write(
                    MAIN_VIEW_HOOK_ADDRESS,
                    MAIN_VIEW_HOOK_ORIGINAL,
                    32,
                )
                hook_installed = False
            except Exception as error:
                cleanup_safe = False
                print(f"WARNING: could not restore main-view hook: {error}")
        if cleanup_safe and client is not None and allocations:
            cleanup_safe = (
                client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
                == MAIN_VIEW_HOOK_ORIGINAL
            )
        if cleanup_safe and client is not None:
            for address in reversed(allocations):
                try:
                    run_heap_bootstrap(client, address, freeing=True)
                except Exception as error:
                    print(
                        f"WARNING: could not release 0x{address:08X}: {error}"
                    )
        elif allocations:
            print(
                "WARNING: retained heap blocks because cleanup was unsafe"
            )
        if client is not None:
            client.close()


if __name__ == "__main__":
    try:
        main()
    except BootstrapCancelled as error:
        print(f"WARNING: {error}", flush=True)
        raise SystemExit(79)
    except BootstrapNotEntered as error:
        print(f"WAITING: {error}", flush=True)
        raise SystemExit(77)
    except BootstrapUncertain as error:
        # A free whose entry is unconfirmed must never be submitted again.
        import sys
        cleanup_request = "--cleanup-code-pointer" in sys.argv
        print(f"{'CRITICAL' if cleanup_request else 'ERROR'}: {error}. Restart the game before another session.", flush=True)
        raise SystemExit(78)
    except (ConnectionError, OSError) as error:
        print(f"Game is no longer reachable: {error}", flush=True)
        raise SystemExit(80)
    except Exception as error:
        print(f"ERROR: {error}", flush=True)
        raise SystemExit(1)
