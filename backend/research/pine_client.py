#!/usr/bin/env python3
"""Small client for PCSX2's local PINE memory/debug protocol."""

from __future__ import annotations

import argparse
import socket
import struct
from pathlib import Path


READ8 = 0
READ16 = 1
READ32 = 2
READ64 = 3
WRITE8 = 4
WRITE16 = 5
WRITE32 = 6
WRITE64 = 7
VERSION = 8
SAVE_STATE = 9
LOAD_STATE = 0x0A
TITLE = 0x0B
GAME_ID = 0x0C
UUID = 0x0D
GAME_VERSION = 0x0E
STATUS = 0x0F


class PineClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 28011, timeout: float = 3.0):
        self.socket = socket.create_connection((host, port), timeout=timeout)

    def close(self) -> None:
        self.socket.close()

    def _recv_exact(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = self.socket.recv(remaining)
            if not chunk:
                raise ConnectionError("PCSX2 closed the PINE connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def request(self, payload: bytes) -> bytes:
        self.socket.sendall(struct.pack("<I", len(payload) + 4) + payload)
        response_size = struct.unpack("<I", self._recv_exact(4))[0]
        if response_size < 5:
            raise ValueError(f"Invalid PINE response length: {response_size}")
        response = self._recv_exact(response_size - 4)
        if response[0] != 0:
            raise RuntimeError("PCSX2 rejected the PINE request")
        return response[1:]

    def text(self, opcode: int) -> str:
        response = self.request(bytes([opcode]))
        size = struct.unpack_from("<I", response, 0)[0]
        raw = response[4 : 4 + size]
        return raw.rstrip(b"\0").decode("utf-8", errors="replace")

    def status(self) -> int:
        return struct.unpack("<I", self.request(bytes([STATUS])))[0]

    def save_state(self, slot: int) -> None:
        if not 0 <= slot <= 255:
            raise ValueError("savestate slot must fit in one byte")
        self.request(bytes([SAVE_STATE, slot]))

    def load_state(self, slot: int) -> None:
        if not 0 <= slot <= 255:
            raise ValueError("savestate slot must fit in one byte")
        self.request(bytes([LOAD_STATE, slot]))

    def read(self, address: int, bits: int) -> int:
        opcodes = {8: READ8, 16: READ16, 32: READ32, 64: READ64}
        formats = {8: "<B", 16: "<H", 32: "<I", 64: "<Q"}
        return struct.unpack(formats[bits], self.request(bytes([opcodes[bits]]) + struct.pack("<I", address)))[0]

    def write(self, address: int, value: int, bits: int) -> None:
        opcodes = {8: WRITE8, 16: WRITE16, 32: WRITE32, 64: WRITE64}
        formats = {8: "<B", 16: "<H", 32: "<I", 64: "<Q"}
        self.request(bytes([opcodes[bits]]) + struct.pack("<I", address) + struct.pack(formats[bits], value))

    def write_block(self, address: int, data: bytes, batch_values: int = 20_000) -> None:
        """Write a contiguous range efficiently using batched 64-bit requests."""
        if address % 8 or len(data) % 8:
            raise ValueError("Block address and size must be aligned to 8 bytes")
        total_values = len(data) // 8
        completed = 0
        while completed < total_values:
            count = min(batch_values, total_values - completed)
            payload = bytearray(count * 13)
            for index in range(count):
                source_offset = (completed + index) * 8
                payload_offset = index * 13
                payload[payload_offset] = WRITE64
                struct.pack_into("<I", payload, payload_offset + 1, address + source_offset)
                payload[payload_offset + 5 : payload_offset + 13] = data[source_offset : source_offset + 8]
            response = self.request(payload)
            if response:
                raise ValueError(f"Unexpected write response bytes: {len(response)}")
            completed += count

    def read_block(
        self,
        address: int,
        size: int,
        batch_values: int = 50_000,
        progress: bool = True,
    ) -> bytes:
        """Read a contiguous range efficiently using batched 64-bit requests."""
        if address % 8 or size % 8:
            raise ValueError("Block address and size must be aligned to 8 bytes")
        result = bytearray(size)
        total_values = size // 8
        completed = 0
        while completed < total_values:
            count = min(batch_values, total_values - completed)
            base = address + completed * 8
            payload = bytearray(count * 5)
            for index in range(count):
                offset = index * 5
                payload[offset] = READ64
                struct.pack_into("<I", payload, offset + 1, base + index * 8)
            response = self.request(payload)
            expected = count * 8
            if len(response) != expected:
                raise ValueError(f"Expected {expected} bytes, received {len(response)}")
            output_offset = completed * 8
            result[output_offset : output_offset + expected] = response
            completed += count
            if progress:
                print(f"read {completed * 8}/{size} bytes", flush=True)
        return bytes(result)

    def read_blocks(
        self, ranges: list[tuple[int, int]]
    ) -> list[bytes]:
        """Read several aligned ranges in one PINE round trip."""
        payload = bytearray()
        sizes: list[int] = []
        for address, size in ranges:
            if address % 8 or size % 8:
                raise ValueError(
                    "Block addresses and sizes must be aligned to 8 bytes"
                )
            sizes.append(size)
            for offset in range(0, size, 8):
                payload.append(READ64)
                payload.extend(struct.pack("<I", address + offset))
        if not payload:
            return []
        response = self.request(bytes(payload))
        expected = sum(sizes)
        if len(response) != expected:
            raise ValueError(
                f"Expected {expected} bytes, received {len(response)}"
            )
        result: list[bytes] = []
        offset = 0
        for size in sizes:
            result.append(response[offset : offset + size])
            offset += size
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--read", type=lambda value: int(value, 0))
    parser.add_argument("--bits", type=int, choices=(8, 16, 32, 64), default=32)
    parser.add_argument("--dump", type=Path)
    parser.add_argument("--write-from", type=Path)
    parser.add_argument("--start", type=lambda value: int(value, 0), default=0)
    parser.add_argument("--size", type=lambda value: int(value, 0), default=0x02000000)
    parser.add_argument("--source-offset", type=lambda value: int(value, 0))
    args = parser.parse_args()

    client = PineClient(port=args.port)
    try:
        if args.dump is not None:
            data = client.read_block(args.start, args.size)
            args.dump.parent.mkdir(parents=True, exist_ok=True)
            args.dump.write_bytes(data)
            print(f"wrote={args.dump} bytes={len(data)}")
            return
        if args.write_from is not None:
            source = args.write_from.read_bytes()
            source_offset = args.start if args.source_offset is None else args.source_offset
            data = source[source_offset : source_offset + args.size]
            if len(data) != args.size:
                raise ValueError("Source file is shorter than the requested region")
            client.write_block(args.start, data)
            print(f"wrote_memory=0x{args.start:08X}-0x{args.start + len(data) - 1:08X} bytes={len(data)} source={args.write_from}")
            return
        if args.read is not None:
            value = client.read(args.read, args.bits)
            print(f"0x{args.read:08X} = 0x{value:0{args.bits // 4}X}")
            return
        statuses = {0: "running", 1: "paused", 2: "shutdown"}
        print(f"emulator={client.text(VERSION)}")
        print(f"status={statuses.get(client.status(), 'unknown')}")
        try:
            print(f"title={client.text(TITLE)}")
            print(f"serial={client.text(GAME_ID)}")
            print(f"crc={client.text(UUID)}")
            print(f"game_version={client.text(GAME_VERSION)}")
        except RuntimeError:
            print("game=<not booted>")
    finally:
        client.close()


if __name__ == "__main__":
    main()
