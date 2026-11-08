#!/usr/bin/env python3
"""Disassemble selected Lua 4.0 chunks stored in The Warriors PS2 WAD.

The binary-chunk layout and opcode table follow the official Lua 4.0.1
sources (lundump.c, lopcodes.h, and print.h).
"""

from __future__ import annotations

import argparse
import collections
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from inspect_warriors_archive import read_root_entries


OPCODES = [
    "END", "RETURN", "CALL", "TAILCALL", "PUSHNIL", "POP", "PUSHINT",
    "PUSHSTRING", "PUSHNUM", "PUSHNEGNUM", "PUSHUPVALUE", "GETLOCAL",
    "GETGLOBAL", "GETTABLE", "GETDOTTED", "GETINDEXED", "PUSHSELF",
    "CREATETABLE", "SETLOCAL", "SETGLOBAL", "SETTABLE", "SETLIST",
    "SETMAP", "ADD", "ADDI", "SUB", "MULT", "DIV", "POW", "CONCAT",
    "MINUS", "NOT", "JMPNE", "JMPEQ", "JMPLT", "JMPLE", "JMPGT",
    "JMPGE", "JMPT", "JMPF", "JMPONT", "JMPONF", "JMP",
    "PUSHNILJMP", "FORPREP", "FORLOOP", "LFORPREP", "LFORLOOP",
    "CLOSURE",
]

NONE_OPS = {0, 13, 23, 25, 26, 27, 28, 30, 31, 43}
U_OPS = {1, 4, 5, 10, 11, 17, 18, 22, 29}
S_OPS = {6, 24}
STRING_OPS = {7, 12, 14, 16, 19}
NUMBER_OPS = {8, 9}
AB_OPS = {2, 3, 20, 21, 48}
JUMP_OPS = set(range(32, 43)) | set(range(44, 48))
MAXARG_S = ((1 << 26) - 1) >> 1


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.endian = "<"
        self.int_size = 4
        self.size_t_size = 4
        self.instruction_size = 4
        self.number_size = 8

    def read(self, size: int) -> bytes:
        result = self.data[self.pos : self.pos + size]
        if len(result) != size:
            raise ValueError(f"Unexpected EOF at 0x{self.pos:X}")
        self.pos += size
        return result

    def byte(self) -> int:
        return self.read(1)[0]

    def uint(self, size: int) -> int:
        return int.from_bytes(self.read(size), "little" if self.endian == "<" else "big")

    def integer(self) -> int:
        return self.uint(self.int_size)

    def size_t(self) -> int:
        return self.uint(self.size_t_size)

    def number(self) -> float:
        if self.number_size == 8:
            return struct.unpack(self.endian + "d", self.read(8))[0]
        if self.number_size == 4:
            return struct.unpack(self.endian + "f", self.read(4))[0]
        raise ValueError(f"Unsupported Lua number size: {self.number_size}")

    def string(self) -> str | None:
        size = self.size_t()
        if size == 0:
            return None
        value = self.read(size)
        if value[-1:] != b"\0":
            raise ValueError(f"Unterminated Lua string at 0x{self.pos - size:X}")
        return value[:-1].decode("latin-1")


@dataclass
class Function:
    source: str | None
    line_defined: int
    num_params: int
    is_vararg: int
    max_stack: int
    locals: list[tuple[str | None, int, int]]
    lines: list[int]
    strings: list[str | None]
    numbers: list[float]
    children: list["Function"]
    code: list[int]


def parse_function(reader: Reader) -> Function:
    source = reader.string()
    line_defined = reader.integer()
    num_params = reader.integer()
    is_vararg = reader.byte()
    max_stack = reader.integer()

    locals_ = []
    for _ in range(reader.integer()):
        locals_.append((reader.string(), reader.integer(), reader.integer()))

    lines = [reader.integer() for _ in range(reader.integer())]
    strings = [reader.string() for _ in range(reader.integer())]
    numbers = [reader.number() for _ in range(reader.integer())]
    children = [parse_function(reader) for _ in range(reader.integer())]
    code = [reader.uint(reader.instruction_size) for _ in range(reader.integer())]
    return Function(
        source, line_defined, num_params, is_vararg, max_stack,
        locals_, lines, strings, numbers, children, code,
    )


def parse_chunk(data: bytes) -> Function:
    reader = Reader(data)
    if reader.read(4) != b"\x1bLua":
        raise ValueError("Not a Lua binary chunk")
    version = reader.byte()
    if version != 0x40:
        raise ValueError(f"Expected Lua 4.0 bytecode, got version 0x{version:02X}")
    reader.endian = "<" if reader.byte() == 1 else ">"
    reader.int_size = reader.byte()
    reader.size_t_size = reader.byte()
    reader.instruction_size = reader.byte()
    instruction_bits = reader.byte()
    opcode_bits = reader.byte()
    b_bits = reader.byte()
    reader.number_size = reader.byte()
    test_number = reader.number()
    if (instruction_bits, opcode_bits, b_bits) != (32, 6, 9):
        raise ValueError("Unsupported Lua 4 instruction layout")
    if int(test_number) != int(3.14159265358979323846e8):
        raise ValueError("Unexpected Lua number format")
    result = parse_function(reader)
    if reader.pos != len(data):
        raise ValueError(f"Trailing data: {len(data) - reader.pos} bytes")
    return result


def read_wad_entries(iso_path: Path) -> tuple[bytes, list[tuple[int, int, int]], int]:
    with iso_path.open("rb") as handle:
        root = read_root_entries(handle)
        dir_entry = root["WARRIORS.DIR"]
        wad_entry = root["WARRIORS.WAD"]
        handle.seek(dir_entry.offset)
        table = handle.read(dir_entry.size)
    count = struct.unpack_from("<I", table, 0)[0]
    entries = [struct.unpack_from("<III", table, 16 + index * 12) for index in range(count)]
    return table, entries, wad_entry.offset


def read_entry(iso_path: Path, entries: list[tuple[int, int, int]], wad_offset: int, index: int) -> bytes:
    relative_offset, size, _third = entries[index]
    with iso_path.open("rb") as handle:
        handle.seek(wad_offset + relative_offset)
        return handle.read(size)


def format_instruction(function: Function, pc: int) -> str:
    instruction = function.code[pc]
    opcode = instruction & 0x3F
    if opcode >= len(OPCODES):
        return f"<BAD_OPCODE_{opcode}> 0x{instruction:08X}"
    name = OPCODES[opcode]
    unsigned = instruction >> 6
    signed = unsigned - MAXARG_S
    arg_b = (instruction >> 6) & 0x1FF
    arg_a = instruction >> 15

    if opcode in NONE_OPS:
        args = ""
    elif opcode in U_OPS:
        args = str(unsigned)
    elif opcode in S_OPS:
        args = str(signed)
    elif opcode in STRING_OPS:
        value = function.strings[unsigned] if unsigned < len(function.strings) else "<bad string index>"
        args = f"{unsigned} ; {value!r}"
    elif opcode in NUMBER_OPS:
        value = function.numbers[unsigned] if unsigned < len(function.numbers) else "<bad number index>"
        args = f"{unsigned} ; {value!r}"
    elif opcode in AB_OPS:
        args = f"{arg_a} {arg_b}"
    elif opcode in JUMP_OPS:
        args = f"{signed} ; to {pc + 2 + signed}"
    else:
        args = f"0x{instruction:08X}"
    return f"{name:<12} {args}".rstrip()


def walk(function: Function, path: str = "0"):
    yield path, function
    for index, child in enumerate(function.children):
        yield from walk(child, f"{path}_{index}")


def matching_pcs(function: Function, needle: str) -> list[int]:
    result = []
    for pc, instruction in enumerate(function.code):
        opcode = instruction & 0x3F
        if opcode not in STRING_OPS:
            continue
        index = instruction >> 6
        if index < len(function.strings) and function.strings[index] == needle:
            result.append(pc)
    return result


def show_matches(index: int, main: Function, needle: str, context: int) -> int:
    count = 0
    for path, function in walk(main):
        pcs = matching_pcs(function, needle)
        for match_pc in pcs:
            count += 1
            print(
                f"entry={index} function={path} source={function.source!r} "
                f"params={function.num_params} stack={function.max_stack} match_pc={match_pc + 1}"
            )
            start = max(0, match_pc - context)
            end = min(len(function.code), match_pc + context + 1)
            for pc in range(start, end):
                marker = ">" if pc == match_pc else " "
                print(f" {marker} {pc + 1:5}: {format_instruction(function, pc)}")
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("iso", type=Path)
    parser.add_argument("--entry", type=int, action="append", default=[])
    parser.add_argument("--find", default="TriggerSphereCfg")
    parser.add_argument("--context", type=int, default=12)
    parser.add_argument("--scan-all", action="store_true")
    parser.add_argument("--list-strings", help="Regex for listing matching Lua string constants")
    parser.add_argument("--show-function", action="append", default=[], help="Function path such as 0_46")
    args = parser.parse_args()

    _table, entries, wad_offset = read_wad_entries(args.iso)
    indexes = range(len(entries)) if args.scan_all else args.entry
    total = 0
    lua_chunks = 0
    listed: dict[str, set[int]] = collections.defaultdict(set)
    string_pattern = re.compile(args.list_strings, re.IGNORECASE) if args.list_strings else None
    with args.iso.open("rb") as handle:
        for index in indexes:
            relative_offset, size, _third = entries[index]
            handle.seek(wad_offset + relative_offset)
            signature = handle.read(4)
            if signature != b"\x1bLua":
                if not args.scan_all:
                    print(f"entry={index}: not Lua bytecode")
                continue
            data = signature + handle.read(size - 4)
            lua_chunks += 1
            try:
                main_function = parse_chunk(data)
            except ValueError as exc:
                print(f"entry={index}: parse error: {exc}")
                continue
            if args.show_function:
                function_map = dict(walk(main_function))
                for path in args.show_function:
                    if path not in function_map:
                        print(f"entry={index}: function path not found: {path}")
                        continue
                    function = function_map[path]
                    print(
                        f"entry={index} function={path} source={function.source!r} "
                        f"params={function.num_params} stack={function.max_stack}"
                    )
                    print(f"  strings={function.strings!r}")
                    print(f"  numbers={function.numbers!r}")
                    for pc in range(len(function.code)):
                        print(f"  {pc + 1:5}: {format_instruction(function, pc)}")
                continue
            if string_pattern:
                for _path, function in walk(main_function):
                    for value in function.strings:
                        if value is not None and string_pattern.search(value):
                            listed[value].add(index)
            else:
                total += show_matches(index, main_function, args.find, args.context)
    if string_pattern:
        for value in sorted(listed, key=str.casefold):
            entry_list = ",".join(str(item) for item in sorted(listed[value]))
            print(f"{value!r}: entries={entry_list}")
        print(f"Scanned {lua_chunks} Lua chunks; found {len(listed)} unique matching strings")
        return
    print(f"Scanned {lua_chunks} Lua chunks; found {total} instruction references to {args.find!r}")


if __name__ == "__main__":
    main()
