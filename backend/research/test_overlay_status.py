import ctypes
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from overlay_status import StatusPublisher, read_status
import map_visibility_control as visibility
from test_map_visibility_control import FakePine


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).parent / f"status_test_{uuid.uuid4().hex}.json"
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))
        self.addCleanup(lambda: self.path.with_suffix(".tmp").unlink(missing_ok=True))
        self.publisher = StatusPublisher(self.path)

    def test_permission_denied_retains_old_json_and_retries_latest_state(self):
        self.publisher.publish({"range_factor": 1})
        with patch.object(Path, "replace", side_effect=PermissionError(5, "locked")) as replace:
            self.assertFalse(self.publisher.publish({"range_factor": 2}))
            self.assertEqual(replace.call_count, 3)
        self.assertEqual(read_status(self.path), {"range_factor": 1})
        self.assertEqual(read_status(self.path, pending=True), {"range_factor": 2})
        self.assertTrue(self.publisher.publish({"range_factor": 8}))
        self.assertEqual(read_status(self.path), {"range_factor": 8})
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_unchanged_state_is_not_republished(self):
        self.publisher.publish({"light": False})
        with patch.object(Path, "replace", side_effect=AssertionError("redundant write")):
            self.assertTrue(self.publisher.publish({"light": False}))

    def test_return_to_old_state_discards_newer_pending_state(self):
        self.publisher.publish({"light": False})
        with patch.object(Path, "replace", side_effect=PermissionError(5, "locked")):
            self.publisher.publish({"light": True})
        self.publisher.publish({"light": False})
        self.assertEqual(read_status(self.path, pending=True), {"light": False})

    def test_temporary_file_write_failure_is_not_fatal(self):
        with patch.object(Path, "write_text", side_effect=PermissionError(5, "locked")):
            self.assertFalse(self.publisher.publish({"light": True}))
        self.assertTrue(self.publisher.publish({"light": True}))

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_real_windows_reader_lock_does_not_crash_and_recovers(self):
        self.publisher.publish({"no_fog": False})
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            self.assertFalse(self.publisher.publish({"no_fog": True}))
            self.assertEqual(read_status(self.path), {"no_fog": False})
        finally:
            kernel.CloseHandle(handle)
        self.assertTrue(self.publisher.publish({"no_fog": True}))
        self.assertEqual(read_status(self.path), {"no_fog": True})

    def test_failed_publication_does_not_exit_freecam_or_prevent_cleanup(self):
        c = FakePine()
        original = dict(c.memory)
        down = set()
        with patch.object(visibility.VisibilityHotkeys, "key_down", side_effect=lambda vk: vk in down), \
                patch("freecam_probe.find_game_window", return_value=1), \
                patch("freecam_probe.game_window_has_focus", return_value=True):
            h = visibility.VisibilityHotkeys(c, self.path, 0x800000)
            with patch.object(Path, "replace", side_effect=PermissionError(5, "locked")):
                down.add(0x58)
                h.poll()
                self.assertTrue(h.controller.fog.owned)
                h.close()
            self.assertEqual(c.memory, original)
            self.assertTrue(read_status(self.path, pending=True)["no_fog"])
            h = visibility.VisibilityHotkeys(c, self.path, 0x800000)
            self.assertTrue(h.preference["no_fog"])
            h.close()

    def test_publication_error_never_masks_native_error(self):
        c = FakePine()
        self.path.write_text(json.dumps({"no_fog": True}))
        with patch.object(visibility.VisibilityHotkeys, "key_down", return_value=False), \
                patch("freecam_probe.find_game_window", return_value=1), \
                patch("freecam_probe.game_window_has_focus", return_value=True):
            h = visibility.VisibilityHotkeys(c, self.path, 0x800000)
            c.memory[visibility.FOG_ADDRESS] = 0xDEADBEEF
            with patch.object(Path, "replace", side_effect=PermissionError(5, "locked")):
                with self.assertRaisesRegex(RuntimeError, "owned by another patch"):
                    h.poll()
                h.close()


if __name__ == "__main__":
    unittest.main()
