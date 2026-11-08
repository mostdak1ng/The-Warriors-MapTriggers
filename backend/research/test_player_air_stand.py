from __future__ import annotations

import struct
import unittest
from unittest import mock

import player_air_stand as air_stand


class FakePineClient:
    def __init__(self, embedded_base: int) -> None:
        self.embedded_base = embedded_base
        self.values: dict[tuple[int, int], int] = {}
        self.writes: list[tuple[int, int, int]] = []
        for hook, original in air_stand.HOOK_SPECS:
            self.values[(hook, 32)] = original

    def read(self, address: int, bits: int) -> int:
        return self.values.get((address, bits), 0)

    def write(self, address: int, value: int, bits: int) -> None:
        self.values[(address, bits)] = value
        self.writes.append((address, value, bits))

    def read_block(
        self, address: int, size: int, *, progress: bool = False
    ) -> bytes:
        marker_address = (
            self.embedded_base
            + air_stand.EMBEDDED_DATA_OFFSET
            + air_stand.MARKER_OFFSET
        )
        if address == marker_address and size == 16:
            return air_stand.MARKER.ljust(16, b"\0")
        return bytes(size)


class PlayerAirStandTests(unittest.TestCase):
    def test_stub_uses_abi_volatile_registers_and_tail_jump(self) -> None:
        base = 0x00800000
        stub = air_stand.build_stub(base, 0)
        words = struct.unpack(f"<{len(stub) // 4}I", stub)

        self.assertLessEqual(len(stub), 0x40)
        self.assertEqual(words[-2], air_stand.jump(air_stand.FALL_FLAG_TEST))
        self.assertEqual(words[-1], 0)
        for word in words:
            registers = (
                (word >> 21) & 31,
                (word >> 16) & 31,
                (word >> 11) & 31,
            )
            self.assertNotIn(26, registers)
            self.assertNotIn(27, registers)

    def test_embedded_layout_stays_inside_reserved_gap(self) -> None:
        base = 0x00800000
        blob = bytearray(0x1000)
        blob[:0x640] = b"\xA5" * 0x640

        air_stand.write_embedded_layout(blob, base)

        self.assertEqual(blob[:0x640], b"\xA5" * 0x640)
        marker_start = (
            air_stand.EMBEDDED_DATA_OFFSET + air_stand.MARKER_OFFSET
        )
        self.assertEqual(
            blob[marker_start : marker_start + len(air_stand.MARKER)],
            air_stand.MARKER,
        )
        for index, stub_offset in enumerate(
            air_stand.EMBEDDED_STUB_OFFSETS
        ):
            stub = air_stand.build_stub(
                base,
                index,
                data_offset=air_stand.EMBEDDED_DATA_OFFSET,
            )
            self.assertLessEqual(
                stub_offset + len(stub),
                air_stand.EMBEDDED_DATA_OFFSET,
            )
            self.assertEqual(
                blob[stub_offset : stub_offset + len(stub)], stub
            )
        self.assertLessEqual(
            air_stand.EMBEDDED_END_OFFSET, 0x800
        )

    def test_embedded_controller_toggles_and_restores_hooks(self) -> None:
        base = 0x00800000
        human = 0x00640C80
        client = FakePineClient(base)

        with mock.patch.object(
            air_stand, "resolve_player_object", return_value=human
        ):
            controller = air_stand.PlayerAirStandController(
                client,
                enabled=True,
                embedded_code_address=base,
            )
            self.assertTrue(controller.enabled)
            self.assertEqual(
                client.read(
                    base
                    + air_stand.EMBEDDED_DATA_OFFSET
                    + air_stand.HUMAN_OFFSET,
                    32,
                ),
                human,
            )
            self.assertEqual(
                client.read(
                    base
                    + air_stand.EMBEDDED_DATA_OFFSET
                    + air_stand.ENABLED_OFFSET,
                    32,
                ),
                1,
            )
            for (hook, _original), stub_offset in zip(
                air_stand.HOOK_SPECS,
                air_stand.EMBEDDED_STUB_OFFSETS,
            ):
                self.assertEqual(
                    client.read(hook, 32),
                    air_stand.jal(base + stub_offset),
                )

            self.assertFalse(controller.toggle())
            controller.close()

        for hook, original in air_stand.HOOK_SPECS:
            self.assertEqual(
                client.read(hook, 32),
                original,
            )
        self.assertEqual(controller.code_address, 0)


if __name__ == "__main__":
    unittest.main()
