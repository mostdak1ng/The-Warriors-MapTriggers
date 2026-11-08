"""Detect the launcher's exit by a retained process handle (no PID reuse)."""
import ctypes
import os

_handle = None
_opened = False


def parent_alive():
    global _handle, _opened
    pid = os.environ.get('TW_MAPTRIGGERS_PARENT_PID')
    if not pid:
        return True
    kernel = ctypes.windll.kernel32
    if not _opened:
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p
        _handle = kernel.OpenProcess(0x100000, False, int(pid))
        _opened = True
    if not _handle:
        return False
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    return kernel.WaitForSingleObject(_handle, 0) == 0x102
