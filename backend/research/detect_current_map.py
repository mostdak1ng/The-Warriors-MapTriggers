#!/usr/bin/env python3
"""Identify the currently loaded map from read-only EE heap evidence."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from disassemble_lua4 import read_entry, read_wad_entries
from pine_client import GAME_ID, PineClient
from player_noclip import read_player_position


# Active mission Lua strings usually occupy this part of the game heap.
# The launcher uses it for a fast first pass, then expands to the complete
# game heap when allocation layout differs between sessions.
DEFAULT_START = 0x00B00000
DEFAULT_END = 0x00E00000
IDENTIFIER_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)


def occurrences(haystack: bytes, needle: bytes) -> int:
    count = 0
    cursor = 0
    while True:
        cursor = haystack.find(needle, cursor)
        if cursor < 0:
            return count
        count += 1
        cursor += max(1, len(needle))


def identifier_occurrences(haystack: bytes, needle: bytes) -> int:
    """Count a token without treating level9 as a match for level99."""
    count = 0
    cursor = 0
    while True:
        cursor = haystack.find(needle, cursor)
        if cursor < 0:
            return count
        end = cursor + len(needle)
        before_is_identifier = (
            cursor > 0 and haystack[cursor - 1] in IDENTIFIER_BYTES
        )
        after_is_identifier = (
            end < len(haystack) and haystack[end] in IDENTIFIER_BYTES
        )
        if not before_is_identifier and not after_is_identifier:
            count += 1
        cursor = end


def identifier_find(haystack: bytes, needle: bytes) -> int:
    """Find a complete identifier and reject numeric-suffix substrings."""
    cursor = 0
    while True:
        cursor = haystack.find(needle, cursor)
        if cursor < 0:
            return -1
        end = cursor + len(needle)
        before_is_identifier = (
            cursor > 0 and haystack[cursor - 1] in IDENTIFIER_BYTES
        )
        after_is_identifier = (
            end < len(haystack) and haystack[end] in IDENTIFIER_BYTES
        )
        if not before_is_identifier and not after_is_identifier:
            return cursor
        cursor = end


def chunk_fingerprints(chunk: bytes) -> list[bytes]:
    """Return longer windows that survive when a raw chunk remains loaded."""
    result: list[bytes] = []
    for offset in (0x40, 0x100, 0x200, 0x400, 0x800):
        if offset + 64 <= len(chunk):
            window = chunk[offset : offset + 64]
            if len(set(window)) >= 8:
                result.append(window)
    return result


def point_to_volume_distance(
    point: tuple[float, float, float], volume: dict
) -> float:
    """Return the shortest distance from a point to a volume's oriented box."""
    origin = volume["origin"]
    size = volume["size"]
    # Lua stores the unrotated minimum corner. Rotation is about the runtime
    # midpoint, as in trigger_overlay.box_corners and the native renderer.
    center = tuple(origin[index] + size[index] * 0.5 for index in range(3))
    delta_x = point[0] - center[0]
    delta_y = point[1] - center[1]
    rotation = volume.get("rotation_xy")
    if rotation is not None:
        # AddVolumeBox stores the local-to-world 2x2 rotation matrix.
        # Its transpose converts the world-space delta back to box-local XY.
        local_x = rotation[0] * delta_x + rotation[2] * delta_y
        local_y = rotation[1] * delta_x + rotation[3] * delta_y
    else:
        local_x = delta_x
        local_y = delta_y
    local_z = point[2] - center[2]
    outside = (
        max(abs(local_x) - size[0] * 0.5, 0.0),
        max(abs(local_y) - size[1] * 0.5, 0.0),
        max(abs(local_z) - size[2] * 0.5, 0.0),
    )
    return math.sqrt(sum(component * component for component in outside))


def scan(
    memory: bytes,
    catalog: dict,
    catalog_directory: Path,
    iso_path: Path | None,
    memory_start: int = 0,
    player_position: tuple[float, float, float] | None = None,
) -> list[dict]:
    entries = list(catalog["entries"])
    name_frequency = Counter(
        name.casefold()
        for item in entries
        for name in item["volume_names"]
    )

    wad_entries = None
    wad_offset = None
    if iso_path is not None:
        _table, wad_entries, wad_offset = read_wad_entries(iso_path)

    results: list[dict] = []
    for item in entries:
        dataset_path = catalog_directory / item["dataset"]
        document = json.loads(dataset_path.read_text(encoding="utf-8"))
        names = [volume["name"] for volume in document["volumes"]]
        distinctive = [
            name
            for name in names
            if len(name) >= 6 and name_frequency[name.casefold()] == 1
        ]
        matched_names = [
            name
            for name in distinctive
            if identifier_find(
                memory, name.encode("latin-1", errors="ignore")
            )
            >= 0
        ]
        matched_all = [
            name
            for name in names
            if identifier_find(
                memory, name.encode("latin-1", errors="ignore")
            )
            >= 0
        ]
        match_addresses = [
            memory_start
            + identifier_find(memory, name.encode("latin-1", errors="ignore"))
            for name in matched_names
        ]

        level_id = item.get("level_id")
        level_hits = 0
        if level_id:
            variants = {
                level_id.encode("ascii"),
                level_id.upper().encode("ascii"),
            }
            level_hits = sum(
                identifier_occurrences(memory, value) for value in variants
            )

        fingerprint_hits = 0
        if iso_path is not None and wad_entries is not None and wad_offset is not None:
            chunk = read_entry(
                iso_path, wad_entries, wad_offset, int(item["entry"])
            )
            fingerprints = chunk_fingerprints(chunk)
            fingerprint_hits = sum(window in memory for window in fingerprints)

        distinctive_ratio = (
            len(matched_names) / len(distinctive) if distinctive else 0.0
        )
        # Live mission scripts expose most or all of their unique volume-name
        # strings. A few old strings or one coincidental raw-code window can
        # remain in freed heap memory, so coverage dominates the ranking.
        score = (
            len(matched_names) * 20
            + distinctive_ratio * 100
            + len(matched_all) * 2
            + min(level_hits, 20)
            + fingerprint_hits * 10
        )
        nearest_volume_distance = None
        nearest_volume_name = None
        nearest_matched_volume_distance = None
        if player_position is not None and document["volumes"]:
            nearest_volume_distance, nearest_volume_name = min(
                (
                    point_to_volume_distance(player_position, volume),
                    volume["name"],
                )
                for volume in document["volumes"]
            )
            nearest_matched_volume_distance = min(
                (
                    point_to_volume_distance(player_position, volume)
                    for volume in document["volumes"]
                    if volume["name"] in matched_names
                ),
                default=None,
            )
        results.append(
            {
                "id": item["id"],
                "entry": item["entry"],
                "display_name": item["display_name"],
                "category": item["category"],
                "score": score,
                "distinctive_hits": len(matched_names),
                "distinctive_total": len(distinctive),
                "all_name_hits": len(matched_all),
                "all_name_total": len(names),
                "level_hits": level_hits,
                "fingerprint_hits": fingerprint_hits,
                "nearest_volume_distance": nearest_volume_distance,
                "nearest_volume_name": nearest_volume_name,
                "nearest_matched_volume_distance": nearest_matched_volume_distance,
                "matched_names": matched_names,
                "match_address_min": min(match_addresses) if match_addresses else None,
                "match_address_max": max(match_addresses) if match_addresses else None,
            }
        )
    results.sort(
        key=lambda row: (
            row["score"],
            row["distinctive_hits"],
            row["all_name_hits"],
        ),
        reverse=True,
    )
    return results


def confident_detection(results: list[dict]) -> tuple[dict | None, str]:
    if not results:
        return None, "no candidates"

    primary_results = [
        row for row in results if row["category"] != "auxiliary"
    ]
    if not primary_results:
        return None, "no primary-map candidates"

    def has_strong_name_evidence(row: dict) -> bool:
        total = row["distinctive_total"]
        hits = row["distinctive_hits"]
        required_hits = min(total, max(3, math.ceil(total * 0.5)))
        coverage = hits / total if total else 0.0
        return hits >= required_hits and coverage >= 0.5

    eligible = [
        row for row in primary_results if has_strong_name_evidence(row)
    ]
    top = primary_results[0]
    runner_up_score = (
        primary_results[1]["score"]
        if len(primary_results) > 1
        else 0.0
    )
    total = top["distinctive_total"]
    hits = top["distinctive_hits"]
    required_hits = min(total, max(3, math.ceil(total * 0.5)))
    coverage = hits / total if total else 0.0
    margin = top["score"] / max(1.0, runner_up_score)

    # Later checkpoints may retain only their own volume names. Come Out To
    # Play's final area has 6/16 names while Coney retains 15/15 distinctive
    # names hundreds of units away. Do not lower the general coverage rule:
    # override a hub only with a unique tokened mission, several distinctive
    # names and decisive proximity to a volume whose name actually matched.
    if top["category"] == "hub" and has_strong_name_evidence(top):
        checkpoint_missions = [
            row
            for row in primary_results
            if row["category"] in {"story", "flashback"}
            and row["level_hits"] > 0
            and row["distinctive_hits"] >= 3
            and row.get("nearest_matched_volume_distance") is not None
            and row["nearest_matched_volume_distance"] <= 10.0
        ]
        if len(checkpoint_missions) == 1:
            mission = checkpoint_missions[0]
            distance = mission["nearest_matched_volume_distance"]
            rivals = [
                row for row in primary_results
                if row is not mission
                and (has_strong_name_evidence(row)
                     or row["distinctive_hits"] >= 3)
            ]
            if rivals and all(
                row.get("nearest_volume_distance") is not None
                and row["nearest_volume_distance"]
                >= max(distance * 3.0, distance + 40.0)
                for row in rivals
            ):
                return mission, (
                    "checkpoint mission names "
                    f"({mission['distinctive_hits']}/"
                    f"{mission['distinctive_total']} unique), level token and "
                    f"player proximity {distance:.1f} units to a matched volume; "
                    "resident hub and other candidates are distant"
                )

    # Story missions can intentionally coexist with their hub script.  In
    # Payin' The Cost, for example, every mission volume name and the live
    # level86 token are present while nearly all of the Coney hub names remain
    # resident too.  Player proximity then favors the hub and its score keeps
    # the ordinary margin below 1.75x.  Complete mission evidence plus a live
    # level token is stronger than that expected resident-hub overlap.
    if (
        top["category"] == "story"
        and total > 0
        and hits == total
        and top["all_name_total"] > 0
        and top["all_name_hits"] == top["all_name_total"]
        and top["level_hits"] > 0
        and margin >= 1.25
    ):
        return top, (
            f"complete mission names ({hits}/{total} unique, "
            f"{top['all_name_hits']}/{top['all_name_total']} all), "
            f"live level token and score margin {margin:.1f}x"
        )

    # A mission script can coexist with a nearly complete resident hub script.
    # Set Up is a particularly tight case: the hub wins the raw name score,
    # even though Set Up has strong name coverage, the live level token and a
    # nearer volume. Resolve only one tokened mission with broad all-name
    # coverage and a comparable score. Overlapping boxes require that every
    # eligible rival is a hub without its own level token.
    tokened_missions = [
        row
        for row in eligible
        if row["category"] in {"story", "flashback"}
        and row["level_hits"] > 0
        and row["all_name_total"] > 0
        and row["all_name_hits"] / row["all_name_total"] >= 0.75
        and row.get("nearest_volume_distance") is not None
    ]
    resident_hubs = [
        row for row in eligible if row["category"] == "hub"
    ]
    if len(tokened_missions) == 1 and resident_hubs:
        mission = tokened_missions[0]
        mission_distance = mission["nearest_volume_distance"]
        rival_distances = [
            row["nearest_volume_distance"]
            for row in eligible
            if row is not mission
            and row.get("nearest_volume_distance") is not None
        ]
        next_distance = min(rival_distances, default=math.inf)
        relative_score = mission["score"] / max(1.0, top["score"])
        untokened_hub_overlap = all(
            row["category"] == "hub"
            and row["level_hits"] == 0
            and row.get("nearest_volume_distance") is not None
            and row["nearest_volume_distance"] > mission_distance
            for row in eligible if row is not mission
        )
        if (
            mission_distance <= 10.0
            and (
                next_distance
                >= max(mission_distance * 3.0, mission_distance + 15.0)
                or untokened_hub_overlap
            )
            and relative_score >= 0.75
        ):
            return mission, (
                "strong mission names "
                f"({mission['distinctive_hits']}/"
                f"{mission['distinctive_total']} unique, "
                f"{mission['all_name_hits']}/"
                f"{mission['all_name_total']} all), live level token and "
                f"player proximity {mission_distance:.1f} units "
                f"(next candidate {next_distance:.1f})"
            )

        # Sparse trigger layouts do not put a box beside every playable spot.
        # Friendly Faces has 13/14 distinctive and 14/15 total names in the
        # live heap, but its nearest box is ~82 units away versus ~376 for
        # Coney. Near-complete names allow relative distance to decide without
        # raising the absolute cutoff for ordinary partial-name candidates.
        matched_distance = mission.get("nearest_matched_volume_distance")
        rivals = [
            row for row in primary_results
            if row is not mission
            and (has_strong_name_evidence(row) or row["distinctive_hits"] >= 3)
        ]
        if (
            mission["distinctive_hits"] >= 3
            and mission["distinctive_hits"] / mission["distinctive_total"] >= 0.9
            and mission["all_name_hits"] / mission["all_name_total"] >= 0.9
            and relative_score >= 0.75
            and matched_distance is not None
            and rivals
            and all(
                row.get("nearest_volume_distance") is not None
                and row["nearest_volume_distance"]
                >= max(matched_distance * 3.0, matched_distance + 40.0)
                for row in rivals
            )
        ):
            next_distance = min(row["nearest_volume_distance"] for row in rivals)
            return mission, (
                "near-complete mission names "
                f"({mission['distinctive_hits']}/"
                f"{mission['distinctive_total']} unique, "
                f"{mission['all_name_hits']}/{mission['all_name_total']} all), "
                f"level token and matched-volume distance {matched_distance:.1f} "
                f"units (next candidate {next_distance:.1f})"
            )

    if eligible:
        spatial = sorted(
            (
                row
                for row in eligible
                if row.get("nearest_volume_distance") is not None
            ),
            key=lambda row: row["nearest_volume_distance"],
        )
        if spatial:
            closest = spatial[0]
            closest_distance = closest["nearest_volume_distance"]
            next_distance = (
                spatial[1]["nearest_volume_distance"]
                if len(spatial) > 1
                else math.inf
            )
            if (
                closest_distance <= 25.0
                and next_distance
                >= max(closest_distance * 3.0, closest_distance + 40.0)
            ):
                return closest, (
                    "strong unique-name coverage "
                    f"({closest['distinctive_hits']}/"
                    f"{closest['distinctive_total']}) and player proximity "
                    f"{closest_distance:.1f} units to "
                    f"{closest['nearest_volume_name']} "
                    f"(next candidate {next_distance:.1f})"
                )

    # Most missions expose dozens of unique names, but a few small Armies of
    # the Night maps contain fewer than three distinctive volume names. Keep
    # the normal half-coverage/three-name threshold for larger maps, while
    # requiring all available evidence when fewer than three names exist.
    if (
        hits >= required_hits
        and coverage >= 0.5
        and margin >= 1.75
    ):
        return top, (
            f"strong unique-name coverage ({hits}/{total}) and "
            f"score margin {margin:.1f}x"
        )
    return None, (
        f"ambiguous: best unique-name coverage {hits}/{total}, "
        f"score margin {margin:.1f}x"
    )


def default_iso_path() -> Path | None:
    """Find the shared ISO above the script, including an isolated test branch.

    Do not depend on the caller's working directory or copy the disc image.
    Without an image, scan() still supports names and player proximity.
    An explicit --iso remains authoritative and reports an invalid path.
    """
    for directory in Path(__file__).resolve().parents:
        candidate = directory / "Warriors, The (USA).iso"
        if candidate.is_file():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("research/lua_volume_catalog.json"),
    )
    parser.add_argument("--iso", type=Path, default=default_iso_path())
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--start", type=lambda value: int(value, 0), default=DEFAULT_START)
    parser.add_argument("--end", type=lambda value: int(value, 0), default=DEFAULT_END)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.start < 0 or args.end <= args.start or (args.end - args.start) % 8:
        raise ValueError("invalid aligned EE scan range")
    if args.top <= 0:
        raise ValueError("top must be positive")

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    client = PineClient(port=args.port)
    player_position = None
    try:
        if client.text(GAME_ID) != "SLUS-21215":
            raise RuntimeError("The Warriors SLUS-21215 is not running")
        try:
            _player, player_position = read_player_position(client)
        except RuntimeError:
            # Name-only detection remains available during unusual player
            # states where no normal WarMoveInstance transform is published.
            pass
        memory = client.read_block(
            args.start, args.end - args.start, progress=False
        )
    finally:
        client.close()

    results = scan(
        memory,
        catalog,
        args.catalog.parent,
        args.iso,
        args.start,
        player_position,
    )
    detected, confidence_reason = confident_detection(results)
    if args.json:
        print(
            json.dumps(
                {
                    "detected": detected,
                    "confidence_reason": confidence_reason,
                    "scan_start": args.start,
                    "scan_end": args.end,
                    "player_position": player_position,
                    "results": results[: args.top],
                },
                indent=2,
            )
        )
        return
    print(
        f"scanned EE 0x{args.start:08X}-0x{args.end:08X}; "
        f"ranked {len(results)} volume datasets"
    )
    for index, row in enumerate(results[: args.top], 1):
        print(
            f"{index:2}. score={row['score']:7.1f} "
            f"unique={row['distinctive_hits']:2}/{row['distinctive_total']:<2} "
            f"all={row['all_name_hits']:2}/{row['all_name_total']:<2} "
            f"level={row['level_hits']:<2} raw={row['fingerprint_hits']} "
            f"{row['display_name']}"
        )
        if row["nearest_volume_distance"] is not None:
            print(
                f"    player_near={row['nearest_volume_distance']:.1f} "
                f"{row['nearest_volume_name']}"
            )
        if row["matched_names"]:
            print(
                f"    heap=0x{row['match_address_min']:08X}-"
                f"0x{row['match_address_max']:08X}  "
                f"{', '.join(row['matched_names'][:10])}"
            )
    if detected is not None:
        print(f"detected={detected['id']} ({confidence_reason})")
    else:
        print(f"detected=<ambiguous> ({confidence_reason})")


if __name__ == "__main__":
    main()
