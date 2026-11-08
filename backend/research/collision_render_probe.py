#!/usr/bin/env python3
"""Render a bounded nearby sample of The Warriors' live collision triangles.

The probe is deliberately separate from the accepted trigger launchers.  It
uses their proven reversible main-world hook and two-block heap lifecycle, but
selects and copies only a small set of nearby collision faces.
"""

from __future__ import annotations

import argparse
import math
import struct
import time
from collections.abc import Callable

from all_volumes_probe import validate_clean_state
from ingame_volume_probe import (
    BLOB_SIZE,
    BOOTSTRAP_ADDRESS,
    BOOTSTRAP_SIZE,
    MARKER,
    MARKER_OFFSET,
    REG,
    RW_IM3D_END,
    RW_IM3D_RENDER_PRIMITIVE,
    RW_IM3D_TRANSFORM,
    RW_MATRIX_SET_IDENTITY,
    FreecamController,
    addiu,
    beq,
    bne,
    call_render_state,
    jal,
    jr,
    load_address,
    lw,
    run_heap_bootstrap,
    sw,
)
from freecam_probe import kind_key_pressed, pressed, wait_for_camera_transform
from ingame_wrapper_probe import (
    HOOK_ADDRESS,
    HOOK_ORIGINAL,
    MAIN_VIEW_HOOK_ADDRESS,
    MAIN_VIEW_HOOK_ORIGINAL,
    MAIN_VIEW_ORIGINAL_CALLEE,
    game_to_render_position,
)
from pine_client import GAME_ID, TITLE, PineClient
from player_noclip import read_player_position


COLLISION_GLOBAL = 0x005DA628
COLLISION_VTABLE = 0x005448B0
DATA_MARKER = b"TW_COLLISION_V1\0"
DATA_HEADER_SIZE = 8
RECORD_SIZE = 40
MAX_RECORDS = (MARKER_OFFSET - DATA_HEADER_SIZE) // RECORD_SIZE
STATUS_OFFSET = 0xF40
MATRIX_OFFSET = 0xF80
DESTINATION_OFFSET = 0x800
VERTICES_PER_TRIANGLE = 3
WALKABLE_NORMAL_Z = math.cos(math.radians(30.0))
COLLISION_FILTER_KEYS = {
    1: "walls",
    2: "all",
    3: "floors",
    4: "walkable",
    5: "steep",
    6: "ceilings",
    7: "query-0x200-excluded",
    8: "solid-one-sided",
    9: "solid-two-sided",
}
COLLISION_FILTERS = tuple(
    dict.fromkeys((*COLLISION_FILTER_KEYS.values(), "human-ignored"))
)
T2 = 10
T3 = 11
T4 = 12
T5 = 13


def align8(value: int) -> int:
    return (value + 7) & ~7


def read_player(client: PineClient) -> tuple[int, tuple[float, float, float]]:
    return read_player_position(client)


def normal(
    vertices: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
) -> tuple[float, float, float]:
    a, b, c = vertices
    ab = tuple(b[index] - a[index] for index in range(3))
    ac = tuple(c[index] - a[index] for index in range(3))
    result = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    length = math.sqrt(sum(component * component for component in result))
    if not length:
        return (0.0, 0.0, 0.0)
    return tuple(component / length for component in result)


def blocks_standard_human(flags: int) -> bool:
    """Match the game's normal human collision query mask (0x200)."""
    return bool(flags & 0x0001) and not bool(flags & 0x0200)


def triangle_matches_filter(
    face_normal: tuple[float, float, float],
    flags: int,
    orientation: str,
    wall_normal_z: float,
) -> bool:
    normal_z = face_normal[2]
    solid = blocks_standard_human(flags)
    two_sided = bool(flags & 0x0002)
    if orientation == "all":
        return True
    if orientation == "walls":
        return abs(normal_z) <= wall_normal_z
    if orientation == "floors":
        return abs(normal_z) >= wall_normal_z
    if orientation == "walkable":
        return solid and normal_z >= WALKABLE_NORMAL_Z
    if orientation == "steep":
        return solid and wall_normal_z < normal_z < WALKABLE_NORMAL_Z
    if orientation == "ceilings":
        return solid and normal_z <= -wall_normal_z
    if orientation in ("query-0x200-excluded", "human-ignored"):
        return not solid
    if orientation == "solid-one-sided":
        return solid and not two_sided
    if orientation == "solid-two-sided":
        return solid and two_sided
    raise ValueError(f"unknown collision filter: {orientation}")


def classify_triangle(row: dict, wall_normal_z: float = 0.35) -> str:
    normal_z = row["normal"][2]
    if not blocks_standard_human(row["flags"]):
        return "query-0x200-excluded"
    if normal_z >= WALKABLE_NORMAL_Z:
        return "walkable"
    if normal_z > wall_normal_z:
        return "steep"
    if normal_z <= -wall_normal_z:
        return "ceiling"
    return "wall"


def describe_triangle(row: dict, wall_normal_z: float = 0.35) -> str:
    side = "two-sided" if row["flags"] & 0x0002 else "one-sided"
    nx, ny, nz = row["normal"]
    query = (
        "included-in-query-0x200"
        if blocks_standard_human(row["flags"])
        else "excluded-from-query-0x200"
    )
    if not blocks_standard_human(row["flags"]):
        observed_role = "alternate-route-blocker-candidate"
    elif row["flags"] & 0x0002:
        observed_role = "two-sided-solid"
    else:
        observed_role = "common-solid-candidate"
    angle = math.degrees(math.acos(max(-1.0, min(1.0, nz))))
    return (
        f"tri={row['index']} class={classify_triangle(row, wall_normal_z)} "
        f"query={query} role={observed_role} {side} "
        f"flags=0x{row['flags']:04X} "
        f"material={row['material']} tail={row['tail']} "
        f"normal=({nx:+.3f},{ny:+.3f},{nz:+.3f}) "
        f"angle_from_up={angle:.1f}deg "
        f"distance={row['distance']:.2f}"
    )


def point_triangle_distance(
    point: tuple[float, float, float],
    triangle: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
) -> float:
    """Shortest Euclidean distance from a point to a triangle surface."""
    a, b, c = triangle

    def subtract(
        left: tuple[float, float, float],
        right: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        return tuple(left[i] - right[i] for i in range(3))  # type: ignore[return-value]

    def dot(
        left: tuple[float, float, float],
        right: tuple[float, float, float],
    ) -> float:
        return sum(left[i] * right[i] for i in range(3))

    def distance_to(
        origin: tuple[float, float, float],
        first: tuple[float, float, float],
        second: tuple[float, float, float],
        first_weight: float,
        second_weight: float,
    ) -> float:
        closest = tuple(
            origin[i]
            + first[i] * first_weight
            + second[i] * second_weight
            for i in range(3)
        )
        return math.dist(point, closest)

    ab = subtract(b, a)
    ac = subtract(c, a)
    ap = subtract(point, a)
    d1 = dot(ab, ap)
    d2 = dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return math.dist(point, a)

    bp = subtract(point, b)
    d3 = dot(ab, bp)
    d4 = dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return math.dist(point, b)

    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        edge_weight = d1 / (d1 - d3)
        return distance_to(a, ab, ac, edge_weight, 0.0)

    cp = subtract(point, c)
    d5 = dot(ab, cp)
    d6 = dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return math.dist(point, c)

    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        edge_weight = d2 / (d2 - d6)
        return distance_to(a, ab, ac, 0.0, edge_weight)

    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        edge_weight = (d4 - d3) / (
            (d4 - d3) + (d5 - d6)
        )
        bc = subtract(c, b)
        return distance_to(b, bc, ac, edge_weight, 0.0)

    denominator = va + vb + vc
    if abs(denominator) < 1.0e-12:
        return min(
            math.dist(point, a),
            math.dist(point, b),
            math.dist(point, c),
        )
    inverse = 1.0 / denominator
    v = vb * inverse
    w = vc * inverse
    return distance_to(a, ab, ac, v, w)


def select_nearest_triangle(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int, int, int, int]],
    point: tuple[float, float, float],
    orientation: str,
    wall_normal_z: float,
) -> list[dict]:
    """Select exactly one filtered face by distance to its actual surface."""
    best: dict | None = None
    for index, (a, b, c, flags, material, tail) in enumerate(triangles):
        points = (vertices[a], vertices[b], vertices[c])
        face_normal = normal(points)
        if not triangle_matches_filter(
            face_normal, flags, orientation, wall_normal_z
        ):
            continue
        distance = point_triangle_distance(point, points)
        if best is not None and distance >= best["distance"]:
            continue
        center = tuple(
            sum(vertex[axis] for vertex in points) / 3.0
            for axis in range(3)
        )
        best = {
            "index": index,
            "points": points,
            "normal": face_normal,
            "center": center,
            "distance": distance,
            "flags": flags,
            "material": material,
            "tail": tail,
        }
    return [best] if best is not None else []


def read_collision(
    client: PineClient,
) -> tuple[
    int,
    list[tuple[float, float, float]],
    list[tuple[int, int, int, int, int, int]],
]:
    collision = client.read(COLLISION_GLOBAL, 32)
    if not 0x00700000 <= collision < 0x02000000:
        raise RuntimeError(f"implausible collision object: 0x{collision:08X}")
    if client.read(collision, 32) != COLLISION_VTABLE:
        raise RuntimeError(
            f"collision vtable mismatch at 0x{collision:08X}"
        )
    triangle_count = client.read(collision + 0x84, 16)
    triangle_pointer = client.read(collision + 0x88, 32)
    vertex_pointer = client.read(collision + 0x8C, 32)
    vertex_count = client.read(collision + 0x90, 32)
    if not 0 < triangle_count < 0x10000:
        raise RuntimeError(f"implausible triangle count: {triangle_count}")
    if not 0 < vertex_count < 0x100000:
        raise RuntimeError(f"implausible vertex count: {vertex_count}")
    for label, pointer in (
        ("triangle", triangle_pointer),
        ("vertex", vertex_pointer),
    ):
        if not 0x00700000 <= pointer < 0x02000000:
            raise RuntimeError(f"implausible {label} pointer: 0x{pointer:08X}")

    vertex_bytes = client.read_block(
        vertex_pointer, align8(vertex_count * 16), progress=False
    )
    triangle_bytes = client.read_block(
        triangle_pointer, align8(triangle_count * 10), progress=False
    )
    vertices = [
        struct.unpack_from("<3f", vertex_bytes, index * 16)
        for index in range(vertex_count)
    ]
    triangles = []
    for index in range(triangle_count):
        fields = struct.unpack_from("<4H2B", triangle_bytes, index * 10)
        if max(fields[:3]) >= vertex_count:
            raise RuntimeError(
                f"triangle {index} has an out-of-range vertex index"
            )
        triangles.append(fields)
    return collision, vertices, triangles


class CollisionSpatialIndex:
    """Precompute static face data and bucket triangle centers for fast queries."""

    def __init__(
        self,
        vertices: list[tuple[float, float, float]],
        triangles: list[tuple[int, int, int, int, int, int]],
        cell_size: float = 20.0,
    ):
        if cell_size <= 0:
            raise ValueError("collision spatial-index cell size must be positive")
        self.cell_size = cell_size
        self.rows: list[dict] = []
        self.cells: dict[tuple[int, int, int], list[int]] = {}
        for index, (a, b, c, flags, material, tail) in enumerate(triangles):
            points = (vertices[a], vertices[b], vertices[c])
            center = tuple(
                sum(point[axis] for point in points) / 3.0
                for axis in range(3)
            )
            row = {
                "index": index,
                "points": points,
                "normal": normal(points),
                "center": center,
                "flags": flags,
                "material": material,
                "tail": tail,
            }
            row_index = len(self.rows)
            self.rows.append(row)
            cell = tuple(
                math.floor(component / self.cell_size)
                for component in center
            )
            self.cells.setdefault(cell, []).append(row_index)

    def select(
        self,
        point: tuple[float, float, float],
        radius: float,
        limit: int,
        predicate: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        if radius > 0:
            origin = tuple(
                math.floor(component / self.cell_size)
                for component in point
            )
            reach = math.ceil(radius / self.cell_size)
            candidate_indices = (
                row_index
                for x in range(origin[0] - reach, origin[0] + reach + 1)
                for y in range(origin[1] - reach, origin[1] + reach + 1)
                for z in range(origin[2] - reach, origin[2] + reach + 1)
                for row_index in self.cells.get((x, y, z), ())
            )
            radius_squared = radius * radius
        else:
            candidate_indices = iter(range(len(self.rows)))
            radius_squared = math.inf

        selected: list[tuple[float, int, dict]] = []
        for row_index in candidate_indices:
            row = self.rows[row_index]
            if predicate is not None and not predicate(row):
                continue
            center = row["center"]
            distance_squared = sum(
                (center[axis] - point[axis]) ** 2 for axis in range(3)
            )
            if distance_squared > radius_squared:
                continue
            selected.append((distance_squared, row["index"], row))
        selected.sort(key=lambda item: (item[0], item[1]))
        if limit > 0:
            selected = selected[:limit]
        return [
            {**row, "distance": math.sqrt(distance_squared)}
            for distance_squared, _index, row in selected
        ]


def select_triangles(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int, int, int, int]],
    player: tuple[float, float, float],
    radius: float,
    limit: int,
    orientation: str,
    wall_normal_z: float,
    *,
    spatial_index: CollisionSpatialIndex | None = None,
) -> list[dict]:
    if spatial_index is not None:
        return spatial_index.select(
            player,
            radius,
            limit,
            lambda row: triangle_matches_filter(
                row["normal"],
                row["flags"],
                orientation,
                wall_normal_z,
            ),
        )
    rows = []
    for index, (a, b, c, flags, material, tail) in enumerate(triangles):
        points = (vertices[a], vertices[b], vertices[c])
        face_normal = normal(points)
        if not triangle_matches_filter(
            face_normal, flags, orientation, wall_normal_z
        ):
            continue
        center = tuple(
            sum(point[axis] for point in points) / 3.0 for axis in range(3)
        )
        distance = math.dist(player, center)
        if radius > 0 and distance > radius:
            continue
        rows.append(
            {
                "index": index,
                "points": points,
                "normal": face_normal,
                "center": center,
                "distance": distance,
                "flags": flags,
                "material": material,
                "tail": tail,
            }
        )
    rows.sort(key=lambda row: row["distance"])
    return rows[:limit] if limit > 0 else rows


def record_color(row: dict, orientation: str) -> tuple[int, int, int, int]:
    if orientation == "walkable":
        return (0, 255, 90, 78)
    if orientation == "steep":
        return (255, 150, 0, 88)
    if orientation == "ceilings":
        return (80, 120, 255, 82)
    if orientation in ("query-0x200-excluded", "human-ignored"):
        return (160, 160, 160, 72)
    if orientation == "solid-one-sided":
        return (255, 0, 210, 82)
    if orientation == "solid-two-sided":
        return (255, 255, 0, 92)
    if orientation == "all" and abs(row["normal"][2]) > 0.35:
        return (0, 190, 255, 44)
    return (255, 0, 210, 92)


def build_data_block(rows: list[dict], orientation: str) -> bytes:
    if len(rows) > MAX_RECORDS:
        raise ValueError(
            f"{len(rows)} triangles exceed the two-block limit of {MAX_RECORDS}"
        )
    blob = bytearray(BLOB_SIZE)
    struct.pack_into("<2I", blob, 0, 1, len(rows))
    for row_index, row in enumerate(rows):
        offset = DATA_HEADER_SIZE + row_index * RECORD_SIZE
        render_positions = [
            game_to_render_position(point) for point in row["points"]
        ]
        struct.pack_into(
            "<9f4B",
            blob,
            offset,
            *(component for point in render_positions for component in point),
            *record_color(row, orientation),
        )
    blob[MARKER_OFFSET : MARKER_OFFSET + len(DATA_MARKER)] = DATA_MARKER
    return bytes(blob)


def update_data_block(
    client: PineClient,
    data_address: int,
    rows: list[dict],
    orientation: str,
    enabled: bool = True,
) -> None:
    """Replace compact records while the EE renderer is temporarily disabled."""
    blob = bytearray(build_data_block(rows, orientation))
    struct.pack_into("<I", blob, 0, 0)
    client.write_block(data_address, bytes(blob))
    if enabled:
        client.write(data_address, 1, 32)


def build_render_code(
    code_address: int, data_address: int, triangle_count: int
) -> bytes:
    status_address = code_address + STATUS_OFFSET
    matrix_address = code_address + MATRIX_OFFSET
    destination_address = code_address + DESTINATION_OFFSET
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

    words.append(jal(MAIN_VIEW_ORIGINAL_CALLEE))
    words.append(0)
    words.append(sw(REG["v0"], 0x2C, REG["sp"]))

    load_address(words, REG["t0"], data_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    disabled = len(words)
    words.extend((0, 0))
    words.append(lw(REG["t1"], 4, REG["t0"]))
    empty = len(words)
    words.extend((0, 0))

    call_render_state(words, 0x10, 7, value=1)
    call_render_state(words, 0x10, 1, value=0)
    call_render_state(words, 0x10, 14, value=0)
    load_address(words, REG["a0"], matrix_address)
    words.append(jal(RW_MATRIX_SET_IDENTITY))
    words.append(0)

    load_address(words, REG["s0"], data_address + DATA_HEADER_SIZE)
    load_address(words, REG["t0"], data_address)
    words.append(lw(REG["s1"], 4, REG["t0"]))
    outer_loop = len(words)
    load_address(words, REG["s2"], destination_address)
    words.append(addiu(REG["t0"], REG["s0"], 0))
    words.append(addiu(REG["t1"], REG["zero"], VERTICES_PER_TRIANGLE))
    words.append(lw(T5, 36, REG["s0"]))
    inner_loop = len(words)
    words.extend(
        (
            lw(T2, 0, REG["t0"]),
            sw(T2, 0, REG["s2"]),
            lw(T3, 4, REG["t0"]),
            sw(T3, 4, REG["s2"]),
            lw(T4, 8, REG["t0"]),
            sw(T4, 8, REG["s2"]),
            sw(T5, 12, REG["s2"]),
            addiu(REG["t0"], REG["t0"], 12),
            addiu(REG["s2"], REG["s2"], 36),
            addiu(REG["t1"], REG["t1"], -1),
        )
    )
    inner_branch = len(words)
    words.append(
        bne(REG["t1"], REG["zero"], inner_loop - (inner_branch + 1))
    )
    words.append(0)

    load_address(words, REG["a0"], destination_address)
    words.append(addiu(REG["a1"], REG["zero"], VERTICES_PER_TRIANGLE))
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 1))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))
    failed = len(words)
    words.extend((0, 0))

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
    triangle_end = len(words)
    words[failed] = beq(
        REG["v0"], REG["zero"], triangle_end - (failed + 1)
    )

    words.append(addiu(REG["s0"], REG["s0"], RECORD_SIZE))
    words.append(addiu(REG["s1"], REG["s1"], -1))
    outer_branch = len(words)
    words.append(
        bne(REG["s1"], REG["zero"], outer_loop - (outer_branch + 1))
    )
    words.append(0)

    call_render_state(words, 0x10, 7, value=2)
    call_render_state(words, 0x10, 1, value=0)
    restore = len(words)
    words[disabled] = beq(
        REG["t1"], REG["zero"], restore - (disabled + 1)
    )
    words[empty] = beq(
        REG["t1"], REG["zero"], restore - (empty + 1)
    )
    words.extend(
        (
            lw(REG["v0"], 0x2C, REG["sp"]),
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


def build_code_block(
    code_address: int, data_address: int, triangle_count: int
) -> tuple[bytes, int]:
    code = build_render_code(code_address, data_address, triangle_count)
    if len(code) > DESTINATION_OFFSET:
        raise ValueError(f"collision render code is too large: 0x{len(code):X}")
    if DESTINATION_OFFSET + VERTICES_PER_TRIANGLE * 36 > STATUS_OFFSET:
        raise ValueError("reusable triangle buffer overlaps status data")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    struct.pack_into("<I", blob, STATUS_OFFSET + 20, triangle_count)
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(blob), len(code)


def cleanup(
    client: PineClient, code_address: int, data_address: int
) -> None:
    code_marker = client.read_block(
        code_address + MARKER_OFFSET, 16, progress=False
    )
    data_marker = client.read_block(
        data_address + MARKER_OFFSET, 16, progress=False
    )
    if code_marker != MARKER:
        raise RuntimeError("collision code marker mismatch")
    if data_marker != DATA_MARKER:
        raise RuntimeError("collision data marker mismatch")
    current_hook = client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
    if current_hook not in (
        MAIN_VIEW_HOOK_ORIGINAL,
        jal(code_address),
    ):
        raise RuntimeError(f"unexpected main-view hook: 0x{current_hook:08X}")
    client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
    if client.read(MAIN_VIEW_HOOK_ADDRESS, 32) != MAIN_VIEW_HOOK_ORIGINAL:
        raise RuntimeError("main-view hook restoration did not verify")
    run_heap_bootstrap(client, data_address, freeing=True)
    run_heap_bootstrap(client, code_address, freeing=True)


def discover_active_pointers(client: PineClient) -> tuple[int, int]:
    hook = client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
    if hook == MAIN_VIEW_HOOK_ORIGINAL:
        raise RuntimeError("the main-view hook is already original")
    if hook >> 26 != 0x03:
        raise RuntimeError(
            f"active main-view instruction is not jal: 0x{hook:08X}"
        )
    code_address = (hook & 0x03FFFFFF) << 2
    if not 0x00700000 <= code_address < 0x01FF0000:
        raise RuntimeError(
            f"decoded code pointer is outside EE heap: 0x{code_address:08X}"
        )
    code_blob = client.read_block(code_address, BLOB_SIZE, progress=False)
    if code_blob[MARKER_OFFSET : MARKER_OFFSET + 16] != MARKER:
        raise RuntimeError(
            f"collision code marker mismatch at 0x{code_address:08X}"
        )

    candidates: set[int] = set()
    words = struct.unpack("<1024I", code_blob)
    for first, second in zip(words, words[1:]):
        if first >> 26 != 0x0F or second >> 26 != 0x09:
            continue
        first_rt = (first >> 16) & 0x1F
        second_rs = (second >> 21) & 0x1F
        second_rt = (second >> 16) & 0x1F
        if first_rt != second_rs or first_rt != second_rt:
            continue
        immediate = second & 0xFFFF
        if immediate & 0x8000:
            immediate -= 0x10000
        value = (((first & 0xFFFF) << 16) + immediate) & 0xFFFFFFFF
        for candidate in (value, value - DATA_HEADER_SIZE):
            if 0x00700000 <= candidate < 0x01FF0000:
                candidates.add(candidate)

    data_matches = []
    for candidate in sorted(candidates):
        try:
            marker = client.read_block(
                candidate + MARKER_OFFSET, 16, progress=False
            )
        except Exception:
            continue
        if marker == DATA_MARKER:
            data_matches.append(candidate)
    if len(data_matches) != 1:
        raise RuntimeError(
            "could not uniquely recover the collision data pointer: "
            + ", ".join(f"0x{address:08X}" for address in data_matches)
        )
    return code_address, data_matches[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--radius", type=float, default=35.0)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument(
        "--orientation", choices=COLLISION_FILTERS, default="walls"
    )
    parser.add_argument(
        "--wall-normal-z",
        type=float,
        default=0.35,
        help="maximum absolute Z normal treated as a wall",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--freecam", action="store_true")
    parser.add_argument("--freecam-speed", type=float, default=8.0)
    parser.add_argument("--mouse-sensitivity", type=float, default=0.14)
    parser.add_argument("--collision-update-hz", type=float, default=8.0)
    parser.add_argument(
        "--camera-stable-seconds",
        type=float,
        default=0.0,
        help="continuous compatible-camera time required before the first EE write",
    )
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--cleanup-code-pointer", type=lambda value: int(value, 0))
    parser.add_argument("--cleanup-data-pointer", type=lambda value: int(value, 0))
    parser.add_argument(
        "--cleanup-active",
        action="store_true",
        help="recover exact pointers from the active hook and clean them",
    )
    args = parser.parse_args()

    if args.duration < 0 or (not args.freecam and args.duration == 0):
        raise ValueError(
            "duration must be positive, or zero only for an unlimited freecam"
        )
    if args.radius < 0 or not 0 < args.limit <= MAX_RECORDS:
        raise ValueError(
            f"radius must be non-negative and limit must be 1-{MAX_RECORDS}"
        )
    if not 0 <= args.wall_normal_z <= 1:
        raise ValueError("wall-normal-z must be between zero and one")
    if args.collision_update_hz <= 0:
        raise ValueError("collision-update-hz must be positive")
    if args.camera_stable_seconds < 0:
        raise ValueError("camera-stable-seconds cannot be negative")
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
    if args.cleanup_active and (
        args.install_only or cleanup_requested or args.dry_run
    ):
        raise ValueError(
            "cleanup-active is mutually exclusive with install, dry-run, "
            "and explicit cleanup"
        )

    client = PineClient(port=args.port)
    allocations: list[int] = []
    hook_installed = False
    freecam: FreecamController | None = None
    try:
        if client.text(GAME_ID) != "SLUS-21215":
            raise RuntimeError("The Warriors SLUS-21215 is not running")
        if args.cleanup_active:
            code_address, data_address = discover_active_pointers(client)
            print(
                f"recovered active collision pointers code="
                f"0x{code_address:08X} data=0x{data_address:08X}"
            )
            cleanup(client, code_address, data_address)
            print("active collision renderer cleaned")
            return
        if cleanup_requested:
            assert args.cleanup_code_pointer is not None
            assert args.cleanup_data_pointer is not None
            cleanup(
                client,
                args.cleanup_code_pointer,
                args.cleanup_data_pointer,
            )
            print(
                f"cleaned collision renderer code="
                f"0x{args.cleanup_code_pointer:08X} data="
                f"0x{args.cleanup_data_pointer:08X}"
            )
            return

        validate_clean_state(client)
        wait_for_camera_transform(
            client, stable_duration=args.camera_stable_seconds
        )
        collision, vertices, triangles = read_collision(client)
        human, player = read_player(client)
        selected = select_triangles(
            vertices,
            triangles,
            player,
            args.radius,
            args.limit,
            args.orientation,
            args.wall_normal_z,
        )
        if not selected:
            raise RuntimeError("no collision triangles matched the selection")
        data_blob = build_data_block(selected, args.orientation)
        _preview, code_size = build_code_block(
            0x00800000, 0x00900000, len(selected)
        )
        print(
            f"game={client.text(TITLE)} collision=0x{collision:08X} "
            f"player_object=0x{human:08X} "
            f"player=({player[0]:.3f},{player[1]:.3f},{player[2]:.3f})"
        )
        print(
            f"mesh triangles={len(triangles)} vertices={len(vertices)} "
            f"selected={len(selected)} orientation={args.orientation} "
            f"radius={args.radius:g} code=0x{code_size:X} "
            f"data=0x{DATA_HEADER_SIZE + len(selected) * RECORD_SIZE:X}"
        )
        for row in selected[:12]:
            print(
                f"  tri={row['index']:4d} distance={row['distance']:6.2f} "
                f"normal_z={row['normal'][2]:6.3f} "
                f"flags=0x{row['flags']:04X} "
                f"material={row['material']:3d} tail={row['tail']:3d}"
            )
        if args.dry_run:
            print("dry-run: validated live selection; no memory was changed")
            return

        code_address = run_heap_bootstrap(client, BLOB_SIZE)
        allocations.append(code_address)
        data_address = run_heap_bootstrap(client, BLOB_SIZE)
        if data_address in allocations:
            raise RuntimeError(f"duplicate EE heap pointer: 0x{data_address:08X}")
        allocations.append(data_address)
        if not all(
            0x00700000 <= address < 0x01FF0000 for address in allocations
        ):
            raise RuntimeError("one or more EE heap pointers are outside range")

        client.write_block(data_address, data_blob)
        code_blob, _code_size = build_code_block(
            code_address, data_address, len(selected)
        )
        client.write_block(code_address, code_blob)
        client.write(MAIN_VIEW_HOOK_ADDRESS, jal(code_address), 32)
        hook_installed = True
        print(
            f"collision render active code=0x{code_address:08X} "
            f"data=0x{data_address:08X}",
            flush=True,
        )

        if args.install_only:
            print(f"ACTIVE_CODE_POINTER=0x{code_address:08X}", flush=True)
            print(f"ACTIVE_DATA_POINTER=0x{data_address:08X}", flush=True)
            hook_installed = False
            allocations.clear()
            return

        if args.freecam:
            freecam = FreecamController(
                client,
                speed=args.freecam_speed,
                mouse_sensitivity=args.mouse_sensitivity,
            )
            print(
                "freecam active: mouse look, WASD move, Q/E down/up, "
                "Shift fast, Ctrl precision",
                flush=True,
            )
            print(
                "collision filters: 0 show/hide, 1 walls, 2 all, "
                "3 floors, 4 walkable, 5 steep, 6 ceilings, "
                "7 alternate-query blockers, 8 common one-sided solids, "
                "9 two-sided solids, "
                "F10 cleanup",
                flush=True,
            )
            deadline = (
                time.monotonic() + args.duration
                if args.duration > 0
                else math.inf
            )
            next_collision_update = 0.0
            update_count = 0
            collision_visible = True
            active_orientation = args.orientation
            filter_key_was_down = [False] * 10
            while time.monotonic() < deadline:
                if pressed("F10"):
                    break
                filter_changed = False
                for key in range(10):
                    down = kind_key_pressed(key)
                    if down and not filter_key_was_down[key]:
                        if key == 0:
                            collision_visible = not collision_visible
                            client.write(
                                data_address, int(collision_visible), 32
                            )
                            state = "ON" if collision_visible else "OFF"
                            print(f"collision: {state}", flush=True)
                        else:
                            active_orientation = COLLISION_FILTER_KEYS[key]
                            filter_changed = True
                            print(
                                f"collision filter: {active_orientation}",
                                flush=True,
                            )
                    filter_key_was_down[key] = down
                freecam.step(client)
                now = time.monotonic()
                if filter_changed or now >= next_collision_update:
                    selected = select_triangles(
                        vertices,
                        triangles,
                        tuple(freecam.position),
                        args.radius,
                        args.limit,
                        active_orientation,
                        args.wall_normal_z,
                    )
                    update_data_block(
                        client,
                        data_address,
                        selected,
                        active_orientation,
                        enabled=collision_visible,
                    )
                    update_count += 1
                    next_collision_update = (
                        now + 1.0 / args.collision_update_hz
                    )
                time.sleep(1.0 / 240.0)
            print(
                f"freecam collision updates={update_count} "
                f"last_selected={len(selected)}",
                flush=True,
            )
            freecam.close()
            freecam = None
        else:
            time.sleep(args.duration)
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
            f"end={status[3]} triangle_draws={status[4]} "
            f"triangles={status[5]}"
        )
    except KeyboardInterrupt:
        pass
    finally:
        if freecam is not None:
            freecam.close()
        cleanup_safe = True
        if hook_installed:
            try:
                client.write(MAIN_VIEW_HOOK_ADDRESS, MAIN_VIEW_HOOK_ORIGINAL, 32)
                hook_installed = False
                print("restored main-view hook")
            except Exception as error:
                cleanup_safe = False
                print(f"WARNING: could not restore main-view hook: {error}")
        if allocations and cleanup_safe:
            try:
                cleanup_safe = (
                    client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
                    == MAIN_VIEW_HOOK_ORIGINAL
                )
            except Exception as error:
                cleanup_safe = False
                print(f"WARNING: could not verify main-view hook: {error}")
        if cleanup_safe:
            for address in reversed(allocations):
                try:
                    run_heap_bootstrap(client, address, freeing=True)
                    print(f"released EE heap block 0x{address:08X}")
                except Exception as error:
                    print(f"WARNING: could not release 0x{address:08X}: {error}")
        elif allocations:
            print("WARNING: retained heap blocks because cleanup was unsafe")
        client.close()


if __name__ == "__main__":
    main()
