#!/usr/bin/env python3
"""Extract statically declared map geometry from Lua 4 bytecode.

Besides ``AddVolumeBox`` calls, this module also recovers literal ``AddFlag``
declarations and the subset of trigger-sphere configuration that can be
associated with a named flag without executing the script.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from disassemble_lua4 import (
    MAXARG_S,
    Function,
    parse_chunk,
    read_entry,
    read_wad_entries,
    walk,
)


@dataclass
class Volume:
    entry: int
    function: str
    instruction: int
    name: str
    kind: int
    origin: list[float]
    size: list[float]
    rotation_xy: list[float] | None


@dataclass
class Flag:
    entry: int
    function: str
    instruction: int
    name: str
    position: list[float]
    parameters: list[float]


@dataclass
class FlagSphereUse:
    entry: int
    function: str
    instruction: int
    flag_name: str
    radius: float
    source: str


DEFAULT_OBJECTIVE_RADIUS = 2.0


def opcode(function: Function, pc: int) -> int:
    return function.code[pc] & 0x3F


def unsigned_arg(function: Function, pc: int) -> int:
    return function.code[pc] >> 6


def string_arg(function: Function, pc: int) -> str | None:
    if opcode(function, pc) not in (7, 12, 14, 16, 19):
        return None
    index = unsigned_arg(function, pc)
    return function.strings[index] if index < len(function.strings) else None


def scalar(function: Function, pc: int) -> float | None:
    op = opcode(function, pc)
    argument = unsigned_arg(function, pc)
    if op == 6:  # PUSHINT
        return float(argument - MAXARG_S)
    if op in (8, 9):  # PUSHNUM / PUSHNEGNUM
        if argument >= len(function.numbers):
            return None
        value = float(function.numbers[argument])
        return -value if op == 9 else value
    return None


def vector_after(function: Function, pc: int) -> tuple[list[float], int] | None:
    if pc >= len(function.code) or opcode(function, pc) != 17:  # CREATETABLE
        return None
    values: list[float] = []
    cursor = pc + 1
    while cursor < len(function.code) and len(values) < 3:
        value = scalar(function, cursor)
        if value is None:
            return None
        values.append(value)
        cursor += 1
    if cursor >= len(function.code) or opcode(function, cursor) != 21:  # SETLIST
        return None
    return values, cursor + 1


def is_global(function: Function, pc: int, name: str) -> bool:
    return opcode(function, pc) == 12 and string_arg(function, pc) == name


def extract_from_function(entry: int, path: str, function: Function) -> list[Volume]:
    result: list[Volume] = []
    add_calls = [
        pc for pc in range(len(function.code))
        if is_global(function, pc, "AddVolumeBox")
    ]
    for call_index, pc in enumerate(add_calls):
        if pc + 4 >= len(function.code) or opcode(function, pc + 1) != 7:
            continue
        name = string_arg(function, pc + 1)
        kind_value = scalar(function, pc + 2)
        first = vector_after(function, pc + 3)
        if name is None or kind_value is None or first is None:
            continue
        origin, cursor = first
        second = vector_after(function, cursor)
        if second is None:
            continue
        size, cursor = second

        next_add = add_calls[call_index + 1] if call_index + 1 < len(add_calls) else len(function.code)
        search_end = min(next_add, cursor + 32)
        rotation: list[float] | None = None
        for rotate_pc in range(cursor, search_end):
            if not is_global(function, rotate_pc, "RotateVolumeBox"):
                continue
            values: list[float] = []
            scan = rotate_pc + 1
            while scan < search_end and opcode(function, scan) != 2:  # CALL
                value = scalar(function, scan)
                if value is not None:
                    values.append(value)
                scan += 1
            if len(values) >= 4:
                rotation = values[-4:]
            break

        result.append(
            Volume(
                entry=entry,
                function=path,
                instruction=pc + 1,
                name=name,
                kind=int(kind_value),
                origin=origin,
                size=size,
                rotation_xy=rotation,
            )
        )
    return result


def extract_flags_from_function(
    entry: int,
    path: str,
    function: Function,
) -> list[Flag]:
    """Recover literal ``AddFlag(name, {x, y, z}, ...)`` declarations."""
    result: list[Flag] = []
    for pc in range(len(function.code)):
        if not is_global(function, pc, "AddFlag"):
            continue
        if pc + 3 >= len(function.code) or opcode(function, pc + 1) != 7:
            continue
        name = string_arg(function, pc + 1)
        position_result = vector_after(function, pc + 2)
        if name is None or position_result is None:
            continue
        position, cursor = position_result
        parameters: list[float] = []
        for scan in range(cursor, min(len(function.code), cursor + 16)):
            if opcode(function, scan) == 2:  # CALL
                break
            value = scalar(function, scan)
            if value is not None:
                parameters.append(value)
        result.append(
            Flag(
                entry=entry,
                function=path,
                instruction=pc + 1,
                name=name,
                position=position,
                parameters=parameters,
            )
        )
    return result


def extract_objective_flag_spheres_from_function(
    entry: int,
    path: str,
    function: Function,
) -> list[FlagSphereUse]:
    """Recover flag-only ObjectiveSetup tables and their effective radii.

    ``ObjectiveAdd`` configures a sphere only when an objective has a ``Flag``
    and no ``Volume``.  A flag supplied alongside a volume is merely the HUD
    marker position and must not be presented as the actual trigger.
    """
    result: list[FlagSphereUse] = []
    for pc in range(len(function.code)):
        if not is_global(function, pc, "ObjectiveSetup"):
            continue
        table_pc = pc + 1
        if table_pc >= len(function.code) or opcode(function, table_pc) != 17:
            continue
        setmap_pc = None
        for scan in range(table_pc + 1, min(len(function.code), table_pc + 192)):
            if opcode(function, scan) == 20:  # SETMAP
                setmap_pc = scan
                break
        if setmap_pc is None:
            continue

        flag_name: str | None = None
        radius: float | None = None
        has_volume = False
        for scan in range(table_pc + 1, setmap_pc):
            if opcode(function, scan) != 7:  # PUSHSTRING
                continue
            key = string_arg(function, scan)
            if key == "Flag" and scan + 1 < setmap_pc:
                if opcode(function, scan + 1) == 12:  # GETGLOBAL
                    flag_name = string_arg(function, scan + 1)
            elif key == "Radius" and scan + 1 < setmap_pc:
                radius = scalar(function, scan + 1)
            elif key == "Volume":
                has_volume = True

        if flag_name is not None and not has_volume:
            result.append(
                FlagSphereUse(
                    entry=entry,
                    function=path,
                    instruction=pc + 1,
                    flag_name=flag_name,
                    radius=(
                        DEFAULT_OBJECTIVE_RADIUS if radius is None else radius
                    ),
                    source="ObjectiveSetup",
                )
            )
    return result


def extract_direct_flag_spheres_from_function(
    entry: int,
    path: str,
    function: Function,
) -> list[FlagSphereUse]:
    """Recover direct ``TriggerSphereCfg(named_flag, ..., radius, ...)`` calls."""
    result: list[FlagSphereUse] = []
    for pc in range(len(function.code)):
        if not is_global(function, pc, "TriggerSphereCfg"):
            continue
        if pc + 3 >= len(function.code) or opcode(function, pc + 1) != 12:
            continue
        flag_name = string_arg(function, pc + 1)
        radius = scalar(function, pc + 3)
        if flag_name is None or radius is None or radius <= 0:
            continue
        result.append(
            FlagSphereUse(
                entry=entry,
                function=path,
                instruction=pc + 1,
                flag_name=flag_name,
                radius=radius,
                source="TriggerSphereCfg",
            )
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("iso", type=Path)
    parser.add_argument("--entry", type=int, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    _table, entries, wad_offset = read_wad_entries(args.iso)
    volumes: list[Volume] = []
    flags: list[Flag] = []
    sphere_uses: list[FlagSphereUse] = []
    for entry in args.entry:
        main_function = parse_chunk(read_entry(args.iso, entries, wad_offset, entry))
        for path, function in walk(main_function):
            volumes.extend(extract_from_function(entry, path, function))
            flags.extend(extract_flags_from_function(entry, path, function))
            sphere_uses.extend(
                extract_objective_flag_spheres_from_function(
                    entry, path, function
                )
            )
            sphere_uses.extend(
                extract_direct_flag_spheres_from_function(entry, path, function)
            )

    document = {
        "format": "the-warriors-lua-map-geometry-v3",
        "count": len(volumes),
        "flag_count": len(flags),
        "trigger_sphere_use_count": len(sphere_uses),
        "volumes": [asdict(volume) for volume in volumes],
        "flags": [asdict(flag) for flag in flags],
        "unassociated_sphere_uses": [asdict(use) for use in sphere_uses],
    }
    encoded = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
        print(
            f"wrote {len(volumes)} volumes, {len(flags)} flags, and "
            f"{len(sphere_uses)} static sphere uses to {args.output}"
        )
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
