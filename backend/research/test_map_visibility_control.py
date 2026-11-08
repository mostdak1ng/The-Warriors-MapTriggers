import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import uuid

import map_visibility_control as visibility

BASE = 0x800000


class FakePine:
    def __init__(self):
        self.memory = {}
        self.writes = []
        self.serial = "SLUS-21215"
        self.fail_address = None
        self.seed(visibility.FOG_ADDRESS, visibility.FOG_ORIGINAL + visibility.FOG_CONTEXT)
        self.seed(visibility.FAR_ADDRESS, visibility.FAR_ORIGINAL + visibility.FAR_CONTEXT)
        for address, original, _, context in visibility.PVS_SPECS:
            self.seed(address, original + context)
        self.seed(visibility.AMBIENT_SIGNATURE_ADDRESS, visibility.AMBIENT_SIGNATURE)
        self.seed(visibility.WORLD_GLOBAL, (0xA00000,))
        self.world(0xA00000)
        self.seed(BASE+visibility.RANGE_STUB_OFFSET, visibility.RANGE_STUB)

    def world(self, pointer):
        self.seed(pointer, (0xB00000, 0xB10000))
        self.seed(pointer + 0x40, (0xB20000,))
        self.seed(pointer + 0x50, struct.unpack("<3I", struct.pack("<3f", .07, .2, 1.5)))

    def seed(self, address, values):
        self.memory.update({address + i * 4: value for i, value in enumerate(values)})

    def text(self, opcode):
        return self.serial if opcode == visibility.GAME_ID else "b99a75de"

    def read(self, address, bits):
        return self.memory.get(address, 0)

    def write(self, address, value, bits):
        if address == self.fail_address:
            self.fail_address = None
            raise OSError("injected one-time failure")
        self.writes.append((address, value))
        self.memory[address] = value

    def write_block(self, address, data):
        for i, value in enumerate(struct.unpack(f"<{len(data)//4}I", data)):
            self.write(address + 4 * i, value, 32)


class VisibilityTests(unittest.TestCase):
    def test_pvs_reapply_restore_and_other_modes_are_independent(self):
        c = FakePine()
        original = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(no_pvs=True)
        self.assertEqual(set(a for a, _ in c.writes), {s[0] for s in visibility.PVS_SPECS})
        for address, stock, replacement, _ in visibility.PVS_SPECS:
            self.assertEqual(c.read(address, 32), replacement[0])
            c.seed(address, stock)  # loading an unmodified save
        controller.apply(no_pvs=True)
        controller.apply()  # suspension or toggle OFF
        self.assertEqual(c.memory, original)
        controller.apply(no_pvs=True, range_factor=8, light=True, no_fog=True)
        controller.close()
        self.assertEqual(c.memory, original)

    def test_pvs_conflict_preflight_and_partial_failure_cleanup(self):
        for index in (0, 1):
            for context in (False, True):
                c = FakePine()
                controller = visibility.VisibilityController(c, BASE)
                address = visibility.PVS_SPECS[index][0] + (4 if context else 0)
                c.memory[address] ^= 1
                with self.assertRaises(RuntimeError):
                    controller.apply(no_pvs=True)
                self.assertEqual(c.writes, [])
        c = FakePine()
        original = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        c.fail_address = visibility.PVS_SPECS[1][0]
        with self.assertRaises(OSError):
            controller.apply(no_pvs=True)
        controller.close()
        self.assertEqual(c.memory, original)

    def test_pvs_close_preserves_foreign_code_and_restores_other_site(self):
        c = FakePine()
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(no_pvs=True)
        address = visibility.PVS_SPECS[1][0]
        c.memory[address] = 0xDEADBEEF
        with self.assertRaises(RuntimeError):
            controller.close()
        self.assertEqual(c.memory[address], 0xDEADBEEF)
        other, original, _, _ = visibility.PVS_SPECS[0]
        self.assertEqual(c.memory[other], original[0])

    def test_default_off_does_not_write_or_require_a_world(self):
        c = FakePine()
        c.serial = "OTHER"
        controller = visibility.VisibilityController(c, BASE)
        controller.apply()
        controller.close()
        self.assertEqual(c.writes, [])

    def test_all_modes_restore_exactly_and_do_not_dim_brighter_channels(self):
        c = FakePine()
        original = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(light=True, no_fog=True, extended=True)
        self.assertEqual(visibility.words(c, 0xA00050, 3), (0x3F800000, 0x3F800000, 0x3FC00000))
        count = len(c.writes)
        controller.apply(light=True, no_fog=True, extended=True)
        self.assertEqual(len(c.writes), count)
        controller.close()
        self.assertEqual(c.memory, original)

    def test_conflicts_and_wrong_game_are_rejected_before_writing(self):
        for address in (visibility.FAR_ADDRESS, visibility.FOG_ADDRESS, visibility.AMBIENT_SIGNATURE_ADDRESS, None):
            c = FakePine()
            controller = visibility.VisibilityController(c, BASE)
            if address is None:
                c.serial = "OTHER"
            else:
                c.memory[address] ^= 1
            with self.assertRaises(RuntimeError):
                controller.apply(light=address == visibility.AMBIENT_SIGNATURE_ADDRESS,
                                 no_fog=address == visibility.FOG_ADDRESS,
                                 extended=address in (visibility.FAR_ADDRESS, None))
            self.assertEqual(c.writes, [])

    def test_does_not_claim_an_existing_identical_patch(self):
        c = FakePine()
        c.seed(visibility.FAR_ADDRESS, visibility.far_variant(BASE, 2))
        controller = visibility.VisibilityController(c, BASE)
        with self.assertRaises(RuntimeError):
            controller.apply(extended=True)
        controller.close()
        self.assertEqual(c.writes, [])

    def test_loaded_stock_state_is_reapplied_then_restored(self):
        c = FakePine()
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(extended=True, no_fog=True)
        c.seed(visibility.FAR_ADDRESS, visibility.FAR_ORIGINAL)
        c.seed(visibility.FOG_ADDRESS, visibility.FOG_ORIGINAL)
        controller.apply(extended=True, no_fog=True)
        self.assertEqual(visibility.words(c, visibility.FAR_ADDRESS, 8), visibility.far_variant(BASE, 2))
        controller.close()
        self.assertEqual(visibility.words(c, visibility.FAR_ADDRESS, 8), visibility.FAR_ORIGINAL)

    def test_new_script_color_is_restored_and_external_changes_are_respected(self):
        c = FakePine()
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(light=True)
        c.memory[0xA00050] = 0x3F000000
        controller.apply(light=True)
        c.memory[0xA00054] = 0x3E800000
        controller.close()
        self.assertEqual(c.memory[0xA00050], 0x3F000000)
        self.assertEqual(c.memory[0xA00054], 0x3E800000)

    def test_world_change_or_recycled_pointer_never_restores_freed_heap(self):
        for recycled in (False, True):
            c = FakePine()
            controller = visibility.VisibilityController(c, BASE)
            controller.apply(light=True)
            if recycled:
                c.memory[0xA00000] = 0xC00000
            else:
                c.memory[visibility.WORLD_GLOBAL] = 0xA10000
                c.world(0xA10000)
            c.writes.clear()
            controller.close()
            self.assertEqual(c.writes, [])

    def test_partial_ambient_write_failure_can_be_cleaned(self):
        c = FakePine()
        before = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        c.fail_address = 0xA00054
        with self.assertRaises(OSError):
            controller.apply(light=True, no_fog=True, extended=True)
        controller.close()
        self.assertEqual(c.memory, before)

    def test_partial_code_transport_failure_can_be_cleaned(self):
        c = FakePine()
        before = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        c.fail_address = visibility.FAR_ADDRESS + 8
        with self.assertRaises(OSError):
            controller.apply(extended=True)
        controller.close()
        self.assertEqual(c.memory, before)

    def test_range_variants_switch_and_restore_complete_wrapper(self):
        c = FakePine()
        original = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        for factor in (2, 4, 8, 4, 2, 1, 8):
            controller.apply(range_factor=factor)
            expected = visibility.far_variant(BASE, factor)
            self.assertEqual(visibility.words(c, visibility.FAR_ADDRESS, 8), expected)
        controller.close()
        self.assertEqual(c.memory, original)

    def test_partial_range_change_can_restore(self):
        c = FakePine()
        original = dict(c.memory)
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(range_factor=4)
        c.fail_address = visibility.FAR_ADDRESS + 8
        with self.assertRaises(OSError):
            controller.apply(range_factor=8)
        controller.close()
        self.assertEqual(c.memory, original)

    def test_invalid_range_rejected_before_any_write(self):
        for factor in (0, 3, -2, 16, 4.0, True, "8"):
            c = FakePine()
            controller = visibility.VisibilityController(c, BASE)
            with self.assertRaises(ValueError):
                controller.apply(light=True, no_fog=True, range_factor=factor)
            self.assertEqual(c.writes, [])

    def test_native_entries_scale_input_and_keep_the_original_stack_wrapper(self):
        for factor in (2,4,8):
            code = visibility.far_variant(BASE,factor)
            self.assertEqual(code[:2], visibility.FAR_ORIGINAL[:2])
            self.assertEqual(code[3:], visibility.FAR_ORIGINAL[3:])
            pc = ((code[2]&0x3FFFFFF)<<2) - (BASE+visibility.RANGE_STUB_OFFSET)
            for authored in (.1,75.,94.,115.,500.):
                value = authored
                index = pc//4
                while visibility.RANGE_STUB[index] == 0x460C6300:
                    value = struct.unpack('<f',struct.pack('<f',value+value))[0]
                    index += 1
                self.assertEqual(visibility.RANGE_STUB[index:], (0x08120AD8,0))
                self.assertAlmostEqual(value, authored*factor, places=5)

    def test_foreign_code_change_on_close_is_not_overwritten_but_other_modes_restore(self):
        c = FakePine()
        controller = visibility.VisibilityController(c, BASE)
        controller.apply(no_fog=True, extended=True)
        c.memory[visibility.FAR_ADDRESS] = 0xDEADBEEF
        with self.assertRaises(RuntimeError):
            controller.close()
        self.assertEqual(c.memory[visibility.FAR_ADDRESS], 0xDEADBEEF)
        self.assertEqual(c.memory[visibility.FOG_ADDRESS], visibility.FOG_ORIGINAL[0])

    def test_hotkeys_focus_edges_transition_and_persistence(self):
        c = FakePine()
        down = set()
        path = Path(__file__).parent / f"visibility_test_{uuid.uuid4().hex}.json"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.addCleanup(lambda: path.with_suffix(".tmp").unlink(missing_ok=True))
        with patch.object(visibility.VisibilityHotkeys, "key_down", side_effect=lambda vk: vk in down), \
                patch("freecam_probe.find_game_window", return_value=1), \
                patch("freecam_probe.game_window_has_focus", return_value=False) as focus:
            h = visibility.VisibilityHotkeys(c, path, BASE)
            self.assertFalse(h.preference["no_pvs"])
            down.add(0x74)
            h.poll()
            focus.return_value = True
            h.poll()  # no synthetic edge when refocusing with a held key
            self.assertEqual(h.preference["range_factor"], 1)
            down.clear()
            h.poll()
            down.add(0x74)
            h.poll()
            self.assertTrue(h.controller.far.owned)
            for factor in (4, 8, 1, 2, 4):
                down.clear()
                h.poll()
                down.add(0x74)
                h.poll()
                h.poll()  # held key must not cycle again
                self.assertEqual(h.preference["range_factor"], factor)
                expected = visibility.far_variant(BASE, factor)
                self.assertEqual(visibility.words(c, visibility.FAR_ADDRESS, 8), expected)
            down.clear()
            h.poll()
            down.add(0x7A)
            h.poll()
            h.poll()  # holding F11 must not toggle again
            self.assertTrue(h.preference["no_pvs"])
            self.assertTrue(all(s.owned for s in h.controller.pvs))
            h.poll(active=False)
            self.assertFalse(h.controller.far.owned)
            self.assertFalse(any(s.owned for s in h.controller.pvs))
            h.close()
            self.assertEqual(json.loads(path.read_text())["range_factor"], 4)
            down.clear()
            h = visibility.VisibilityHotkeys(c, path, BASE)
            h.poll()
            self.assertTrue(h.controller.far.owned)
            self.assertEqual(h.preference["range_factor"], 4)
            self.assertTrue(h.preference["no_pvs"])
            self.assertTrue(all(s.owned for s in h.controller.pvs))
            h.close()


if __name__ == "__main__":
    unittest.main()
