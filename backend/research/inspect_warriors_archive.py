#!/usr/bin/env python3
"""Read-only structural inspector for The Warriors PS2 WARRIORS.DIR/WAD.

The script reads both files directly from an ISO9660 image. It does not mount,
extract, or modify the image.
"""

from __future__ import annotations

import argparse
import collections
import re
import statistics
import struct
from dataclasses import dataclass
from pathlib import Path


SECTOR_SIZE = 2048
INTERESTING = re.compile(
    rb"(?i)[\x20-\x7e]{0,80}"
    rb"(?:trigger|collision|collide|camera|level|mission|script|portal|volume|zone|boundary|spawn|checkpoint)"
    rb"[\x20-\x7e]{0,120}"
)


@dataclass(frozen=True)
class IsoEntry:
    name: str
    extent: int
    size: int

    @property
    def offset(self) -> int:
        return self.extent * SECTOR_SIZE


def read_root_entries(handle) -> dict[str, IsoEntry]:
    handle.seek(16 * SECTOR_SIZE)
    pvd = handle.read(SECTOR_SIZE)
    if len(pvd) != SECTOR_SIZE or pvd[1:6] != b"CD001":
        raise ValueError("Not an ISO9660 primary volume descriptor")

    root = pvd[156 : 156 + pvd[156]]
    root_extent = struct.unpack_from("<I", root, 2)[0]
    root_size = struct.unpack_from("<I", root, 10)[0]
    handle.seek(root_extent * SECTOR_SIZE)
    data = handle.read(root_size)

    entries: dict[str, IsoEntry] = {}
    cursor = 0
    while cursor < len(data):
        record_length = data[cursor]
        if record_length == 0:
            cursor = ((cursor // SECTOR_SIZE) + 1) * SECTOR_SIZE
            continue
        record = data[cursor : cursor + record_length]
        extent = struct.unpack_from("<I", record, 2)[0]
        size = struct.unpack_from("<I", record, 10)[0]
        name_length = record[32]
        raw_name = record[33 : 33 + name_length]
        if raw_name not in (b"\x00", b"\x01"):
            name = raw_name.decode("ascii", errors="replace").split(";", 1)[0]
            entries[name.upper()] = IsoEntry(name=name, extent=extent, size=size)
        cursor += record_length
    return entries


def ascii_preview(data: bytes, limit: int = 5) -> list[str]:
    strings = re.findall(rb"[\x20-\x7e]{5,}", data)
    return [item[:120].decode("ascii", errors="replace") for item in strings[:limit]]


def inspect(
    iso_path: Path,
    sample_bytes: int,
    match_limit: int,
    detail_entries: list[int],
) -> None:
    with iso_path.open("rb") as handle:
        iso_entries = read_root_entries(handle)
        dir_entry = iso_entries["WARRIORS.DIR"]
        wad_entry = iso_entries["WARRIORS.WAD"]

        print("ISO files:")
        print(f"  WARRIORS.DIR extent={dir_entry.extent} offset=0x{dir_entry.offset:X} size={dir_entry.size}")
        print(f"  WARRIORS.WAD extent={wad_entry.extent} offset=0x{wad_entry.offset:X} size={wad_entry.size}")

        handle.seek(dir_entry.offset)
        table = handle.read(dir_entry.size)
        file_count = struct.unpack_from("<I", table, 0)[0]
        expected = 16 + file_count * 12
        if expected != len(table):
            raise ValueError(f"DIR length mismatch: expected {expected}, got {len(table)}")

        archive_entries = [struct.unpack_from("<III", table, 16 + index * 12) for index in range(file_count)]
        sizes = [size for _offset, size, _flags in archive_entries]
        flags = collections.Counter(flag for _offset, _size, flag in archive_entries)
        print(f"Archive entries: {file_count}")
        print(
            "Sizes: "
            f"min={min(sizes)} median={int(statistics.median(sizes))} "
            f"mean={int(statistics.mean(sizes))} max={max(sizes)} total={sum(sizes)}"
        )
        print("Third-field values:")
        for value, count in flags.most_common(20):
            print(f"  0x{value:08X}: {count}")

        if detail_entries:
            for index in detail_entries:
                if not 0 <= index < file_count:
                    raise ValueError(f"Entry index out of range: {index}")
                relative_offset, size, third_field = archive_entries[index]
                handle.seek(wad_entry.offset + relative_offset)
                data = handle.read(size)
                print(f"Entry {index}:")
                print(
                    f"  offset=0x{relative_offset:08X} size={size} "
                    f"third=0x{third_field:08X}"
                )
                print(f"  first_128={data[:128].hex(' ')}")
                print("  ASCII strings:")
                for item in re.findall(rb"[\x20-\x7e]{4,}", data):
                    print(f"    {item.decode('ascii', errors='replace')}")
            return

        magic_counts: collections.Counter[bytes] = collections.Counter()
        interesting_matches: list[tuple[int, int, int, str]] = []
        preview_rows: list[tuple[int, int, int, bytes, list[str]]] = []

        for index, (relative_offset, size, _flags) in enumerate(archive_entries):
            if relative_offset + size > wad_entry.size:
                raise ValueError(f"Entry {index} exceeds WAD bounds")
            handle.seek(wad_entry.offset + relative_offset)
            sample = handle.read(min(size, sample_bytes))
            magic = sample[:4].ljust(4, b"\0")
            magic_counts[magic] += 1

            if index < 30:
                preview_rows.append((index, relative_offset, size, magic, ascii_preview(sample)))

            if len(interesting_matches) < match_limit:
                for match in INTERESTING.finditer(sample):
                    text = match.group().decode("ascii", errors="replace").strip()
                    interesting_matches.append((index, relative_offset + match.start(), size, text))
                    if len(interesting_matches) >= match_limit:
                        break

        print("Most common 4-byte signatures:")
        for magic, count in magic_counts.most_common(30):
            ascii_magic = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in magic)
            print(f"  {magic.hex(' ')}  {ascii_magic!r}: {count}")

        print("First 30 entries:")
        for index, offset, size, magic, strings in preview_rows:
            print(f"  {index:5} off=0x{offset:08X} size={size:9} magic={magic.hex(' ')} strings={strings}")

        print(f"Interesting strings (first {match_limit}, scanning up to {sample_bytes} bytes per entry):")
        if not interesting_matches:
            print("  <none>")
        for index, offset, size, value in interesting_matches:
            print(f"  entry={index:5} entry_off=0x{offset:08X} size={size:9}  {value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("iso", type=Path)
    parser.add_argument("--sample-bytes", type=int, default=65536)
    parser.add_argument("--match-limit", type=int, default=300)
    parser.add_argument("--entry", type=int, action="append", default=[])
    args = parser.parse_args()
    inspect(args.iso, args.sample_bytes, args.match_limit, args.entry)


if __name__ == "__main__":
    main()
