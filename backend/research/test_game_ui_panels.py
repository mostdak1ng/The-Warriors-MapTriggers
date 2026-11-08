import struct
import unittest
from pathlib import Path
from unittest.mock import Mock

import game_ui_panels as ui
from world_lifecycle import wait_for_world_ready


class FakePine:
    def __init__(self):
        self.words = list(ui.CONTEXT)
        self.fade_words = list(ui.FADE_CONTEXT)
        self.writes = []
        self.serial = "SLUS-21215"

    def text(self, opcode):
        return self.serial if opcode == ui.GAME_ID else "b99a75de"

    def read_block(self, address, size, **kwargs):
        words = self.words if address == ui.CONTEXT_ADDRESS else self.fade_words
        return struct.pack("<4I", *words)

    def read(self, address, bits):
        words = self.words if address == ui.PATCH_ADDRESS else self.fade_words
        return words[2]

    def write(self, address, value, bits):
        self.writes.append((address, value, bits))
        words = self.words if address == ui.PATCH_ADDRESS else self.fade_words
        words[2] = value


class PanelsTests(unittest.TestCase):
    def test_default_off_never_writes_and_toggle_restores(self):
        client = FakePine()
        controller = ui.GameUiPanels(client)
        controller.close()
        self.assertFalse(controller.hidden)
        self.assertEqual(client.writes, [])
        controller.set_hidden(True)
        controller.set_hidden(True)
        self.assertEqual(len(client.writes), 2)
        self.assertEqual(client.fade_words[2], ui.FADE_HIDDEN)
        controller.close()
        self.assertEqual(client.words, list(ui.CONTEXT))
        self.assertEqual(client.fade_words, list(ui.FADE_CONTEXT))
        self.assertFalse(controller.hidden)

    def test_wrong_game_or_context_never_writes(self):
        for corrupt in ("game", "context", "owned", "fade_context", "fade_owned"):
            with self.subTest(corrupt=corrupt):
                client = FakePine()
                if corrupt == "game":
                    client.serial = "OTHER"
                elif corrupt == "context":
                    client.words[3] = 0
                elif corrupt == "fade_context":
                    client.fade_words[0] = 0
                elif corrupt == "fade_owned":
                    client.fade_words[2] = ui.FADE_HIDDEN
                else:
                    client.words[2] = ui.HIDDEN
                controller = ui.GameUiPanels(client)
                with self.assertRaises(RuntimeError):
                    controller.set_hidden(True)
                controller.close()
                self.assertEqual(client.writes, [])

    def test_savestate_original_can_be_reapplied_or_closed(self):
        client = FakePine()
        controller = ui.GameUiPanels(client)
        controller.set_hidden(True)
        client.words[2] = ui.ORIGINAL
        controller.set_hidden(True)
        self.assertEqual(client.words[2], ui.HIDDEN)
        client.words[2] = ui.ORIGINAL
        client.fade_words[2] = ui.FADE_ORIGINAL
        count = len(client.writes)
        controller.close()
        self.assertEqual(len(client.writes), count)

    def test_cleanup_does_not_overwrite_foreign_code(self):
        client = FakePine()
        controller = ui.GameUiPanels(client)
        controller.set_hidden(True)
        client.words[2] = 0xDEADBEEF
        with self.assertRaises(RuntimeError):
            controller.close()
        self.assertEqual(client.words[2], 0xDEADBEEF)
        self.assertEqual(client.fade_words, list(ui.FADE_CONTEXT))

    def test_failed_second_write_rolls_back_both_sites(self):
        client = FakePine()
        original_write = client.write

        def write(address, value, bits):
            original_write(address, value, bits)
            if address == ui.FADE_PATCH_ADDRESS and value == ui.FADE_HIDDEN:
                raise OSError("simulated partial transport failure")

        client.write = write
        controller = ui.GameUiPanels(client)
        with self.assertRaises(OSError):
            controller.set_hidden(True)
        self.assertEqual(client.words, list(ui.CONTEXT))
        self.assertEqual(client.fade_words, list(ui.FADE_CONTEXT))
        self.assertFalse(controller.hidden)

    def test_menu_wait_can_exit_without_player_or_memory_access(self):
        stop = Mock(spec=Path)
        stop.exists.return_value = True
        with self.assertRaises(InterruptedError):
            wait_for_world_ready(object(), stop_file=stop)


if __name__ == "__main__":
    unittest.main()
