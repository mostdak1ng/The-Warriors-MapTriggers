#!/usr/bin/env python3
"""Catalog every Lua 4 chunk in the WAD that declares map geometry.

This is a read-only discovery tool.  It scans archive entry headers, parses
Lua chunks, and applies the static volume/flag extraction used by
``extract_lua_volumes.py``.  Objective trigger spheres declared in checkpoint
chunks are associated back to the map that owns each named flag.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from disassemble_lua4 import parse_chunk, read_wad_entries, walk
from extract_lua_volumes import (
    Flag,
    FlagSphereUse,
    Volume,
    extract_direct_flag_spheres_from_function,
    extract_flags_from_function,
    extract_from_function,
    extract_objective_flag_spheres_from_function,
    opcode,
    string_arg,
)


def extract_gstring_table(functions: list[tuple[str, object]], name: str) -> dict[str, str]:
    """Recover literal GSTRING.<name>[key] = value assignments."""
    result: dict[str, str] = {}
    for _path, function in functions:
        for pc in range(len(function.code) - 4):
            if not (
                opcode(function, pc) == 12
                and string_arg(function, pc) == "GSTRING"
                and opcode(function, pc + 1) == 14
                and string_arg(function, pc + 1) == name
                and opcode(function, pc + 2) == 7
                and opcode(function, pc + 3) == 7
                and opcode(function, pc + 4) == 20
            ):
                continue
            key = string_arg(function, pc + 2)
            value = string_arg(function, pc + 3)
            if key is not None and value is not None:
                result[key.lower()] = value
    return result


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return result or "volumes"


def classify(level_id: str | None, mission: str, location_label: str) -> tuple[str, int, str]:
    prefix, _, location = location_label.partition(":")
    location = location or location_label.lstrip(":")
    if prefix.isdigit() and 1 <= int(prefix) <= 18:
        number = int(prefix)
        return "story", number, f"{number:02d}. {mission} - {location}"
    if len(prefix) == 1 and prefix in "ABCDE":
        order = ord(prefix) - ord("A") + 1
        return "flashback", order, f"Flashback {prefix}. {mission} - {location}"
    if mission.lower().startswith("stage "):
        try:
            order = int(mission.split()[-1])
        except ValueError:
            order = 99
        return "armies", order, f"Armies of the Night {mission} - {location}"
    if level_id == "level95":
        return "hub", 1, "Coney hub"
    label = mission or level_id or "unidentified level"
    if location:
        label = f"{label} - {location}"
    return "other", 999, label


def scan(
    iso_path: Path,
) -> tuple[dict, dict[int, list[Volume]], dict[int, list[dict]]]:
    _table, entries, wad_offset = read_wad_entries(iso_path)
    lua_count = 0
    parse_errors: list[dict[str, object]] = []
    volume_entries: list[dict[str, object]] = []
    extracted: dict[int, list[Volume]] = {}
    extracted_flags: dict[int, list[Flag]] = {}
    sphere_uses: list[tuple[FlagSphereUse, set[str]]] = []
    english_mission_names: dict[str, str] = {}
    english_level_names: dict[str, str] = {}
    label_source_entry: int | None = None

    with iso_path.open("rb") as handle:
        for entry, (relative_offset, size, third) in enumerate(entries):
            if size < 4:
                continue
            handle.seek(wad_offset + relative_offset)
            if handle.read(4) != b"\x1bLua":
                continue
            lua_count += 1
            handle.seek(wad_offset + relative_offset)
            data = handle.read(size)
            try:
                main_function = parse_chunk(data)
            except (UnicodeDecodeError, ValueError) as error:
                parse_errors.append({"entry": entry, "error": str(error)})
                continue

            functions = list(walk(main_function))
            mission_names = extract_gstring_table(functions, "MISSIONNAME")
            level_names = extract_gstring_table(functions, "LEVELNAME")
            if (
                mission_names.get("level9") == "All-City"
                and level_names.get("level61") == ":Lower Manhattan"
                and any(
                    value == "Restart level"
                    for _path, function in functions
                    for value in function.strings
                )
            ):
                english_mission_names = mission_names
                english_level_names = level_names
                label_source_entry = entry
            strings = {
                value
                for _path, function in functions
                for value in function.strings
                if value
            }
            level_ids_for_chunk = {
                value.lower()
                for value in strings
                if re.fullmatch(r"level\d+", value, re.IGNORECASE)
            }
            volumes = []
            flags: list[Flag] = []
            for path, function in functions:
                volumes.extend(extract_from_function(entry, path, function))
                flags.extend(extract_flags_from_function(entry, path, function))
                for use in extract_objective_flag_spheres_from_function(
                    entry, path, function
                ):
                    sphere_uses.append((use, level_ids_for_chunk))
                for use in extract_direct_flag_spheres_from_function(
                    entry, path, function
                ):
                    sphere_uses.append((use, level_ids_for_chunk))
            if flags:
                extracted_flags[entry] = flags
            if not volumes:
                continue
            extracted[entry] = volumes

            sources = sorted(
                {
                    function.source
                    for _path, function in functions
                    if function.source
                }
            )
            kinds = Counter(volume.kind for volume in volumes)
            volume_entries.append(
                {
                    "entry": entry,
                    "archive_size": size,
                    "archive_third": third,
                    "sources": sources,
                    "level_ids": sorted(
                        level_ids_for_chunk
                    ),
                    "function_count": len(functions),
                    "volume_count": len(volumes),
                    "kind_counts": {
                        str(kind): count for kind, count in sorted(kinds.items())
                    },
                    "volume_names": [volume.name for volume in volumes],
                }
            )

    category_order = {
        "story": 0,
        "flashback": 1,
        "armies": 2,
        "hub": 3,
        "other": 4,
        "auxiliary": 5,
    }
    for item in volume_entries:
        level_ids = item["level_ids"]
        level_id = level_ids[0] if len(level_ids) == 1 else None
        mission = english_mission_names.get(level_id or "", "")
        location_label = english_level_names.get(level_id or "", "")
        if level_id:
            category, category_index, display_name = classify(
                level_id, mission, location_label
            )
            identifier = slug(f"{level_id}_{mission or location_label}")
        else:
            category = "auxiliary"
            category_index = int(item["entry"])
            samples = item["volume_names"][:3]
            sample_text = ", ".join(samples)
            display_name = f"Auxiliary entry {item['entry']} - {sample_text}"
            identifier = slug(f"entry_{item['entry']}_{samples[0]}")
        item.update(
            {
                "id": identifier,
                "level_id": level_id,
                "mission_name": mission,
                "location_label": location_label,
                "category": category,
                "category_index": category_index,
                "display_name": display_name,
                "dataset": f"volumes/{identifier}.json",
            }
        )

    declaration_entries: dict[str, set[int]] = {}
    for entry, flags in extracted_flags.items():
        for flag in flags:
            declaration_entries.setdefault(flag.name.casefold(), set()).add(entry)

    associated_flags: dict[int, list[dict]] = {}
    for item in volume_entries:
        entry = int(item["entry"])
        item_level_ids = set(item["level_ids"])
        rows: list[dict] = []
        for flag in extracted_flags.get(entry, []):
            flag_key = flag.name.casefold()
            uses = []
            for use, use_level_ids in sphere_uses:
                if use.flag_name.casefold() != flag_key:
                    continue
                same_entry = use.entry == entry
                same_level = bool(item_level_ids & use_level_ids)
                globally_unique = len(declaration_entries.get(flag_key, ())) == 1
                if same_entry or same_level or globally_unique:
                    uses.append(asdict(use))
            row = asdict(flag)
            row["sphere_uses"] = uses
            rows.append(row)
        associated_flags[entry] = rows
        item["flag_count"] = len(rows)
        item["trigger_flag_count"] = sum(
            bool(row["sphere_uses"]) for row in rows
        )

    volume_entries.sort(
        key=lambda item: (
            category_order[str(item["category"])],
            int(item["category_index"]),
            int(item["entry"]),
        )
    )
    document = {
        "format": "the-warriors-lua-map-geometry-catalog-v2",
        "archive_entry_count": len(entries),
        "lua_entry_count": lua_count,
        "volume_entry_count": len(volume_entries),
        "volume_count": sum(
            int(item["volume_count"]) for item in volume_entries
        ),
        "flag_count": sum(int(item["flag_count"]) for item in volume_entries),
        "trigger_flag_count": sum(
            int(item["trigger_flag_count"]) for item in volume_entries
        ),
        "label_source_entry": label_source_entry,
        "parse_errors": parse_errors,
        "entries": volume_entries,
    }
    return document, extracted, associated_flags


def write_datasets(
    document: dict,
    extracted: dict[int, list[Volume]],
    associated_flags: dict[int, list[dict]],
    output_directory: Path,
) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    for item in document["entries"]:
        volumes = extracted[int(item["entry"])]
        flags = associated_flags[int(item["entry"])]
        dataset = {
            "format": "the-warriors-lua-map-geometry-v3",
            "count": len(volumes),
            "flag_count": len(flags),
            "trigger_flag_count": sum(
                bool(flag["sphere_uses"]) for flag in flags
            ),
            "entry": item["entry"],
            "id": item["id"],
            "level_id": item["level_id"],
            "display_name": item["display_name"],
            "volumes": [asdict(volume) for volume in volumes],
            "flags": flags,
        }
        path = output_directory / f"{item['id']}.json"
        path.write_text(
            json.dumps(dataset, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("iso", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args()

    document, extracted, associated_flags = scan(args.iso)
    if args.output_directory:
        write_datasets(
            document, extracted, associated_flags, args.output_directory
        )
    encoded = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
        print(
            f"wrote {document['volume_count']} volumes from "
            f"{document['volume_entry_count']} Lua entries plus "
            f"{document['flag_count']} flags "
            f"({document['trigger_flag_count']} trigger flags) to {args.output}"
        )
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
