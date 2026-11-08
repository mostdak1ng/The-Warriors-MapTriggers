#!/usr/bin/env python3
"""Reversible in-game RenderWare line-box probe for The Warriors (SLUS-21215).

The probe hooks the end of one world-render pass, submits an untextured
RwIm3D line list with depth testing enabled, and restores the original hook
instruction on exit.  The injected code/data remain inert after restoration.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import struct
import time
from pathlib import Path

from freecam_probe import (
    Point,
    find_game_window,
    normalize,
    normalize_quaternion,
    pressed,
    quaternion_axis_angle,
    quaternion_multiply,
    rotate,
    resolve_camera_transform,
    window_center,
)
from pine_client import GAME_ID, TITLE, PineClient


HOOK_ADDRESS = 0x00159418
HOOK_ORIGINAL = 0x0060F809  # jalr $ra, $v1 (DevRWGraphicsDevice::EndScene)
BOOTSTRAP_HOOK_ADDRESS = 0x001567C4
BOOTSTRAP_HOOK_ORIGINAL = 0x0C05F0C2
BLOB_SIZE = 0x1000
STATUS_OFFSET = 0x7C0
VERTEX_OFFSET = 0x800
MARKER_OFFSET = BLOB_SIZE - 16
MARKER = b"TW_IG_VOL_V1\0\0\0\0"

# The ELF has a 0x70-byte alignment gap between .vutext and .data.  It is
# large enough for a one-frame allocator/free bootstrap, but not for the
# renderer itself.  The full renderer lives in memory obtained from the
# game's own heap and is released on exit.
BOOTSTRAP_ADDRESS = 0x0050A810
BOOTSTRAP_SIZE = 0x70
BOOTSTRAP_RESULT = 0x0050A874
BOOTSTRAP_ENTRY = BOOTSTRAP_RESULT + 4
BOOTSTRAP_DONE = BOOTSTRAP_RESULT + 8
BOOTSTRAP_FRAME = 0x0050C654
GAME_MALLOC = 0x00433B60
GAME_FREE = 0x00433BB0

RW_RENDER_STATE_TABLE = 0x0070AD28
RW_IM3D_TRANSFORM = 0x004A4F28
RW_IM3D_RENDER_PRIMITIVE = 0x004A49E8
RW_IM3D_END = 0x004A4FD0
RW_MATRIX_SET_IDENTITY = 0x00198658
ORIGINAL_CALLEE = 0x0017C308

# RenderWare states changed for the line pass. Values use the game's own
# conventions, observed in its existing immediate-mode renderers.
RENDER_STATES = (
    (1, 0),   # texture raster = null
    (6, 1),   # Z test enabled
    (8, 0),   # Z writes disabled
    (10, 5),  # source blend = source alpha
    (11, 6),  # destination blend = inverse source alpha
    (12, 1),  # vertex alpha enabled
    (14, 0),  # fog disabled for debug lines
    (20, 1),  # no back-face culling
)

# Stable semantic palette.  Color depends only on the Lua volume kind; names
# such as "Trigger" or "Turf" never override it.
KIND_COLORS = {
    0: (255, 85, 77, 255),
    1: (85, 255, 120, 255),
    2: (255, 226, 74, 255),
    3: (53, 215, 255, 255),
}
UNKNOWN_KIND_COLOR = (255, 255, 255, 255)


class FreecamController:
    """The proven mouse/WASD freecam, sharing this probe's PINE client."""

    def __init__(
        self,
        client: PineClient,
        speed: float = 8.0,
        mouse_sensitivity: float = 0.14,
    ) -> None:
        self.camera_transform = resolve_camera_transform(client)
        raw = client.read_block(self.camera_transform, 0x60, progress=False)
        values = list(struct.unpack_from("<8f", raw))
        self.position = values[:3]
        self.position_w = values[3]
        self.quaternion = normalize_quaternion(tuple(values[4:8]))
        self.speed = speed
        self.mouse_sensitivity = mouse_sensitivity
        self.world_up = [0.0, 0.0, 1.0]
        self.game_window = find_game_window()
        self.original_cursor = Point()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(self.original_cursor))
        self.mouse_engaged = False
        self.previous = time.monotonic()

    def step(self, client: PineClient) -> None:
        now = time.monotonic()
        dt = min(now - self.previous, 0.05)
        self.previous = now
        speed = self.speed
        if pressed("SHIFT"):
            speed *= 4.0
        if pressed("CTRL"):
            speed *= 0.25

        yaw = 0.0
        pitch = 0.0
        look_step = math.radians(90.0) * dt
        if pressed("H"):
            yaw += look_step
        if pressed("K"):
            yaw -= look_step
        if pressed("U"):
            pitch += look_step
        if pressed("J"):
            pitch -= look_step

        foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        center = window_center(self.game_window)
        if self.game_window and foreground == self.game_window and center is not None:
            cursor = Point()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
            if self.mouse_engaged:
                delta_x = cursor.x - center[0]
                delta_y = cursor.y - center[1]
                if abs(delta_x) < 1000 and abs(delta_y) < 1000:
                    yaw -= math.radians(delta_x * self.mouse_sensitivity)
                    pitch -= math.radians(delta_y * self.mouse_sensitivity)
            ctypes.windll.user32.SetCursorPos(*center)
            self.mouse_engaged = True
        else:
            self.mouse_engaged = False

        if yaw:
            rotation = quaternion_axis_angle((0.0, 0.0, 1.0), yaw)
            self.quaternion = normalize_quaternion(
                quaternion_multiply(rotation, self.quaternion)
            )
        if pitch:
            rotation = quaternion_axis_angle((1.0, 0.0, 0.0), pitch)
            candidate = normalize_quaternion(
                quaternion_multiply(self.quaternion, rotation)
            )
            if abs(rotate(candidate, (0.0, 1.0, 0.0))[2]) < 0.995:
                self.quaternion = candidate

        right = normalize(rotate(self.quaternion, (1.0, 0.0, 0.0)))
        forward = normalize(rotate(self.quaternion, (0.0, 1.0, 0.0)))
        movement = [0.0, 0.0, 0.0]
        for key, axis, sign in (
            ("W", forward, 1.0),
            ("S", forward, -1.0),
            ("D", right, 1.0),
            ("A", right, -1.0),
            ("E", self.world_up, 1.0),
            ("Q", self.world_up, -1.0),
        ):
            if pressed(key):
                for index in range(3):
                    movement[index] += axis[index] * sign
        length = math.sqrt(sum(component * component for component in movement))
        if length:
            for index in range(3):
                self.position[index] += movement[index] / length * speed * dt

        client.write_block(
            self.camera_transform,
            struct.pack(
                "<8f", *self.position, self.position_w, *self.quaternion
            ),
        )

    def close(self) -> None:
        if self.mouse_engaged:
            ctypes.windll.user32.SetCursorPos(
                self.original_cursor.x, self.original_cursor.y
            )

REG = {
    "zero": 0,
    "v0": 2,
    "v1": 3,
    "a0": 4,
    "a1": 5,
    "a2": 6,
    "a3": 7,
    "t0": 8,
    "t1": 9,
    "s0": 16,
    "s1": 17,
    "s2": 18,
    "t9": 25,
    "sp": 29,
    "ra": 31,
}


def i_type(opcode: int, rs: int, rt: int, immediate: int) -> int:
    return (opcode << 26) | (rs << 21) | (rt << 16) | (immediate & 0xFFFF)


def addiu(rt: int, rs: int, immediate: int) -> int:
    return i_type(0x09, rs, rt, immediate)


def lui(rt: int, immediate: int) -> int:
    return i_type(0x0F, 0, rt, immediate)


def lw(rt: int, offset: int, base: int) -> int:
    return i_type(0x23, base, rt, offset)


def sw(rt: int, offset: int, base: int) -> int:
    return i_type(0x2B, base, rt, offset)


def jal(target: int) -> int:
    return 0x0C000000 | ((target >> 2) & 0x03FFFFFF)


def jalr(register: int) -> int:
    return (register << 21) | (REG["ra"] << 11) | 0x09


def jr(register: int) -> int:
    return (register << 21) | 0x08


def beq(rs: int, rt: int, displacement_words: int) -> int:
    return i_type(0x04, rs, rt, displacement_words)


def bne(rs: int, rt: int, displacement_words: int) -> int:
    return i_type(0x05, rs, rt, displacement_words)


def load_address(words: list[int], register: int, address: int) -> None:
    upper = (address + 0x8000) >> 16
    words.append(lui(register, upper))
    words.append(addiu(register, register, address & 0xFFFF))


def call_render_state(
    words: list[int],
    function_offset: int,
    state: int,
    value: int | None = None,
    stack_offset: int | None = None,
) -> None:
    load_address(words, REG["t0"], RW_RENDER_STATE_TABLE)
    words.append(lw(REG["t9"], function_offset, REG["t0"]))
    words.append(addiu(REG["a0"], REG["zero"], state))
    if stack_offset is not None:
        words.append(addiu(REG["a1"], REG["sp"], stack_offset))
    elif value is not None:
        words.append(addiu(REG["a1"], REG["zero"], value))
    else:
        raise ValueError("render-state call requires a value or stack address")
    words.append(jalr(REG["t9"]))
    words.append(0)


class BootstrapNotEntered(TimeoutError):
    """The hook retired without executing the operation; retry is safe."""


class BootstrapUncertain(TimeoutError):
    """Entered shim did not retire; retain its code and never retry a free."""


class BootstrapCancelled(InterruptedError):
    """Stopped before retirement; keep executable scratch intact."""


def _build_heap_shim(pointers=(), *, freeing=False, probe=False):
    # The final jump calls the displaced native function with the caller's
    # original SP/RA. Its delay slot publishes completion: the EE can never
    # return into the alignment gap after that store. Entry is separate from
    # both the malloc result and completion, and remains latched on reentry.
    words = [addiu(REG["sp"], REG["sp"], -0x20),
             sw(REG["ra"], 0x10, REG["sp"])]
    load_address(words, REG["t0"], BOOTSTRAP_RESULT)
    words.append(lw(REG["v0"], 4, REG["t0"]))
    branch = len(words)
    words.extend([0, addiu(REG["v0"], REG["zero"], 1),
                  sw(REG["v0"], 4, REG["t0"])])
    for pointer in pointers:
        load_address(words, REG["a0"], pointer)
        words.extend([jal(GAME_FREE if freeing else GAME_MALLOC), 0])
    if not freeing and not probe:
        load_address(words, REG["t0"], BOOTSTRAP_RESULT)
        words.append(sw(REG["v0"], 0, REG["t0"]))
    finish = len(words)
    load_address(words, REG["t0"], BOOTSTRAP_RESULT)
    words.extend([addiu(REG["v0"], REG["zero"], 1),
                  lw(REG["ra"], 0x10, REG["sp"]),
                  addiu(REG["sp"], REG["sp"], 0x20),
                  0x08000000 | (ORIGINAL_CALLEE >> 2),
                  sw(REG["v0"], 8, REG["t0"])])
    words[branch] = bne(REG["v0"], REG["zero"], finish-(branch+1))
    code = struct.pack(f"<{len(words)}I", *words)
    if len(code) > BOOTSTRAP_RESULT-BOOTSTRAP_ADDRESS:
        raise ValueError("heap shim exceeds allocator alignment gap")
    return code.ljust(BOOTSTRAP_SIZE, b"\0")


def build_heap_bootstrap(size_or_pointer: int, freeing: bool = False) -> bytes:
    return _build_heap_shim((size_or_pointer,), freeing=freeing)


def build_heap_callsite_probe() -> bytes:
    return _build_heap_shim(probe=True)


def build_heap_free_pair_bootstrap(first_pointer: int, second_pointer: int) -> bytes:
    return _build_heap_shim((first_pointer, second_pointer), freeing=True)


_f10_armed = False


def bootstrap_cancelled():
    global _f10_armed
    import os
    from freecam_probe import game_window_has_focus
    down = pressed("F10")
    if not down:
        _f10_armed = True
    elif _f10_armed and game_window_has_focus(find_game_window()):
        return True
    from session_guard import parent_alive
    if not parent_alive():
        return True
    cancel = os.environ.get("TW_MAPTRIGGERS_CANCEL_FILE")
    return bool(cancel and Path(cancel).exists())


def _run_heap_shim(client, code, timeout, *, probe=False, on_wait=None):
    if timeout <= 0:
        raise ValueError("heap shim timeout must be positive")
    if client.read(BOOTSTRAP_HOOK_ADDRESS, 32) != BOOTSTRAP_HOOK_ORIGINAL:
        raise RuntimeError("allocator bootstrap hook is unexpectedly occupied")
    original_gap = client.read_block(BOOTSTRAP_ADDRESS, BOOTSTRAP_SIZE, progress=False)
    if any(original_gap):
        raise RuntimeError("ELF bootstrap alignment gap is unexpectedly occupied")
    installed = jal(BOOTSTRAP_ADDRESS)
    client.write_block(BOOTSTRAP_ADDRESS, code)
    try:
        client.write(BOOTSTRAP_HOOK_ADDRESS, installed, 32)
        if client.read(BOOTSTRAP_HOOK_ADDRESS, 32) != installed:
            raise RuntimeError("allocator bootstrap hook did not verify")
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if client.read(BOOTSTRAP_DONE, 32):
                break
            if bootstrap_cancelled() or (on_wait is not None and on_wait()):
                break
            time.sleep(0.01)
    finally:
        # Never erase executable bytes here. A fetched JAL or an allocator
        # callee can still return to this shim even after detaching the hook.
        current = client.read(BOOTSTRAP_HOOK_ADDRESS, 32)
        if current not in (installed, BOOTSTRAP_HOOK_ORIGINAL):
            raise BootstrapUncertain("allocator hook ownership lost; scratch retained")
        client.write(BOOTSTRAP_HOOK_ADDRESS, BOOTSTRAP_HOOK_ORIGINAL, 32)

    # A completion sampled before detachment might belong to a previous
    # invocation while a second has entered its prologue. Reset only DONE,
    # never ENTRY/result (which prevent duplicate malloc/free on reentry).
    client.write(BOOTSTRAP_DONE, 0, 32)
    frame = client.read(BOOTSTRAP_FRAME, 32)
    deadline = time.monotonic()+3.0
    notice = False
    while True:
        done = client.read(BOOTSTRAP_DONE, 32)
        advanced = (client.read(BOOTSTRAP_FRAME, 32)-frame) & 0xFFFFFFFF
        if done or 2 <= advanced < 0x80000000:
            entered = client.read(BOOTSTRAP_ENTRY, 32)
            result = client.read(BOOTSTRAP_RESULT, 32)
            client.write_block(BOOTSTRAP_ADDRESS, original_gap)
            if not entered:
                if probe:
                    return False
                raise BootstrapNotEntered("Allocator pass inactive; no operation executed")
            return True if probe else result
        if bootstrap_cancelled() or (on_wait is not None and on_wait()):
            raise BootstrapCancelled("Exit cancelled while waiting; executable scratch retained. Restart the game before another session.")
        if time.monotonic() >= deadline:
            if not probe and client.read(BOOTSTRAP_ENTRY, 32):
                raise BootstrapUncertain("Entered allocator shim did not retire; scratch retained")
            if not notice:
                print("Close the game's pause menu or resume PCSX2 to finish exiting" if probe else
                      "Waiting for a complete stable gameplay world... Close the game's pause menu or resume PCSX2.", flush=True)
                notice = True
        time.sleep(0.01)


def probe_heap_callsite(client: PineClient, timeout: float = 0.1, on_wait=None) -> bool:
    return _run_heap_shim(client, build_heap_callsite_probe(), timeout, probe=True, on_wait=on_wait)


def run_heap_free_pair_bootstrap(client, first_pointer, second_pointer, timeout=3.0):
    try:
        _run_heap_shim(client, build_heap_free_pair_bootstrap(first_pointer, second_pointer), timeout)
    except BootstrapNotEntered:
        raise
    except (ConnectionError, OSError):
        raise
    except Exception as error:
        raise BootstrapUncertain(f"EE heap free-pair did not confirm completion: {error}") from error


def run_heap_bootstrap(client, size_or_pointer, freeing=False, timeout=3.0):
    result = _run_heap_shim(client, build_heap_bootstrap(size_or_pointer, freeing), timeout)
    if not freeing and not result:
        raise RuntimeError("EE heap allocation returned null")
    return result


def build_code(
    vertex_address: int,
    vertex_count: int,
    matrix_address: int,
    status_address: int,
    primitive_type: int,
) -> bytes:
    words: list[int] = []
    words.append(addiu(REG["sp"], REG["sp"], -0x40))
    words.append(sw(REG["ra"], 0x30, REG["sp"]))
    words.append(sw(REG["v1"], 0x2C, REG["sp"]))

    # Five diagnostic words: executions, Transform, RenderPrimitive, End,
    # and the original EndScene method address received in $v1.
    load_address(words, REG["t0"], status_address)
    words.append(lw(REG["t1"], 0, REG["t0"]))
    words.append(addiu(REG["t1"], REG["t1"], 1))
    words.append(sw(REG["t1"], 0, REG["t0"]))
    words.append(lw(REG["t1"], 0x2C, REG["sp"]))
    words.append(sw(REG["t1"], 16, REG["t0"]))

    # Match the state sequence used by the game's own RwIm3D renderer at
    # 0x001831C0.  The normal queued-debug pass runs immediately afterward.
    for state, value in RENDER_STATES:
        call_render_state(words, 0x10, state, value=value)

    load_address(words, REG["a0"], matrix_address)
    words.append(jal(RW_MATRIX_SET_IDENTITY))
    words.append(0)

    load_address(words, REG["a0"], vertex_address)
    words.append(addiu(REG["a1"], REG["zero"], vertex_count))
    load_address(words, REG["a2"], matrix_address)
    words.append(addiu(REG["a3"], REG["zero"], 0))
    words.append(jal(RW_IM3D_TRANSFORM))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 4, REG["t0"]))

    branch_index = len(words)
    words.append(0)  # beq v0, zero, finish
    words.append(0)
    words.append(addiu(REG["a0"], REG["zero"], primitive_type))
    words.append(jal(RW_IM3D_RENDER_PRIMITIVE))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 8, REG["t0"]))
    words.append(jal(RW_IM3D_END))
    words.append(0)
    load_address(words, REG["t0"], status_address)
    words.append(sw(REG["v0"], 12, REG["t0"]))

    finish_index = len(words)
    words.append(lw(REG["t9"], 0x2C, REG["sp"]))
    words.append(jalr(REG["t9"]))
    words.append(0)

    words.append(lw(REG["ra"], 0x30, REG["sp"]))
    words.append(addiu(REG["sp"], REG["sp"], 0x40))
    words.append(jr(REG["ra"]))
    words.append(0)

    displacement = finish_index - (branch_index + 1)
    words[branch_index] = beq(REG["v0"], REG["zero"], displacement)
    return struct.pack(f"<{len(words)}I", *words)


def box_corners(volume: dict) -> list[tuple[float, float, float]]:
    origin = volume["origin"]
    size = volume["size"]
    center = [origin[index] + size[index] * 0.5 for index in range(3)]
    extents = [component * 0.5 for component in size]
    a, b, c, d = volume.get("rotation_xy") or [1.0, 0.0, 0.0, 1.0]
    result = []
    for index in range(8):
        x = extents[0] if index & 1 else -extents[0]
        y = extents[1] if index & 2 else -extents[1]
        z = extents[2] if index & 4 else -extents[2]
        result.append((center[0] + a * x + b * y, center[1] + c * x + d * y, center[2] + z))
    return result


def build_vertices(volume: dict, solid: bool = False) -> bytes:
    corners = box_corners(volume)
    if solid:
        quads = (
            (0, 1, 3, 2),
            (4, 6, 7, 5),
            (0, 2, 6, 4),
            (1, 5, 7, 3),
            (0, 4, 5, 1),
            (2, 3, 7, 6),
        )
        corner_indices = [
            index
            for a, b, c, d in quads
            for index in (a, b, c, a, c, d)
        ]
    else:
        edges = [
            (index, index ^ bit)
            for index in range(8)
            for bit in (1, 2, 4)
            if index < (index ^ bit)
        ]
        corner_indices = [index for edge in edges for index in edge]
    color = KIND_COLORS.get(volume["kind"], UNKNOWN_KIND_COLOR)
    if solid:
        color = (*color[:3], 72)
    output = bytearray()
    for corner_index in corner_indices:
        x, y, z = corners[corner_index]
        output.extend(
            struct.pack("<3f4B2f3I", x, y, z, *color, 0.0, 0.0, 0, 0, 0)
        )
    return bytes(output)


def build_blob(
    volume: dict, cave_address: int, solid: bool = False
) -> tuple[bytes, int, int]:
    vertices = build_vertices(volume, solid=solid)
    vertex_address = cave_address + VERTEX_OFFSET
    status_address = cave_address + STATUS_OFFSET
    matrix_offset = (VERTEX_OFFSET + len(vertices) + 15) & ~15
    matrix_address = cave_address + matrix_offset
    code = build_code(
        vertex_address,
        len(vertices) // 36,
        matrix_address,
        status_address,
        3 if solid else 1,
    )
    if len(code) > VERTEX_OFFSET:
        raise ValueError(f"injected code is too large: 0x{len(code):X}")
    identity = struct.pack(
        "<16f",
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 0.0,
    )
    if matrix_offset + len(identity) > MARKER_OFFSET:
        raise ValueError("vertices do not fit in the injection blob")
    blob = bytearray(BLOB_SIZE)
    blob[: len(code)] = code
    blob[VERTEX_OFFSET : VERTEX_OFFSET + len(vertices)] = vertices
    blob[matrix_offset : matrix_offset + len(identity)] = identity
    blob[MARKER_OFFSET : MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(blob), len(code), len(vertices) // 36


def find_volume(document: dict, name: str) -> dict:
    for volume in document["volumes"]:
        if volume["name"].lower() == name.lower():
            return volume
    raise ValueError(f"volume not found: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("volumes", type=Path, nargs="?", default=Path("research/all_city_volumes.json"))
    parser.add_argument("--volume", default="vbTrainShack")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds; 0 waits until Ctrl+C")
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--heap-test", action="store_true")
    parser.add_argument("--freecam", action="store_true")
    parser.add_argument("--freecam-speed", type=float, default=8.0)
    parser.add_argument("--mouse-sensitivity", type=float, default=0.14)
    parser.add_argument(
        "--solid",
        action="store_true",
        help="draw translucent triangle faces instead of one-pixel lines",
    )
    parser.add_argument("--restore", action="store_true", help="restore only the original hook instruction")
    args = parser.parse_args()

    document = json.loads(args.volumes.read_text(encoding="utf-8"))
    volume = find_volume(document, args.volume)
    _preview, code_size, vertex_count = build_blob(
        volume, 0x00800000, solid=args.solid
    )

    client = PineClient(port=args.port)
    installed = False
    allocated = 0
    render_status = (0, 0, 0, 0, 0)
    freecam: FreecamController | None = None
    try:
        serial = client.text(GAME_ID)
        if serial != "SLUS-21215":
            raise RuntimeError(f"unexpected game serial: {serial}")
        current_hook = client.read(HOOK_ADDRESS, 32)
        if args.restore:
            client.write(HOOK_ADDRESS, HOOK_ORIGINAL, 32)
            print(f"restored hook 0x{HOOK_ADDRESS:08X} to 0x{HOOK_ORIGINAL:08X}")
            return
        if current_hook != HOOK_ORIGINAL:
            raise RuntimeError(
                f"hook mismatch at 0x{HOOK_ADDRESS:08X}: 0x{current_hook:08X}"
            )
        bootstrap_gap = client.read_block(
            BOOTSTRAP_ADDRESS, BOOTSTRAP_SIZE, progress=False
        )
        if any(bootstrap_gap):
            raise RuntimeError("ELF bootstrap alignment gap is unexpectedly occupied")
        print(
            f"game={client.text(TITLE)} volume={volume['name']} code=0x{code_size:X} "
            f"vertices={vertex_count} hook=0x{HOOK_ADDRESS:08X}"
        )
        if args.dry_run:
            print("dry-run: validation passed; no memory was changed")
            return

        allocated = run_heap_bootstrap(client, BLOB_SIZE)
        if not 0x00700000 <= allocated < 0x01FF0000:
            raise RuntimeError(f"unexpected EE heap pointer: 0x{allocated:08X}")
        if args.heap_test:
            print(f"heap-test: allocated 0x{allocated:08X}")
            return
        blob, code_size, vertex_count = build_blob(
            volume, allocated, solid=args.solid
        )
        hook_patched = jal(allocated)
        # Heap-backed data/code first, hook last. Restoring the hook makes the
        # allocation inert before it is returned to the game's heap.
        client.write_block(allocated, blob)
        client.write(HOOK_ADDRESS, hook_patched, 32)
        installed = True
        if args.freecam:
            freecam = FreecamController(
                client,
                speed=args.freecam_speed,
                mouse_sensitivity=args.mouse_sensitivity,
            )
        print(
            f"in-game line box active at 0x{allocated:08X}; "
            "Ctrl+C restores the original hook",
            flush=True,
        )
        deadline = time.monotonic() + args.duration if args.duration > 0 else float("inf")
        next_heartbeat = time.monotonic()
        while time.monotonic() < deadline:
            if freecam is not None:
                freecam.step(client)
                time.sleep(1.0 / 240.0)
            else:
                now = time.monotonic()
                if now >= next_heartbeat:
                    live_hook = client.read(HOOK_ADDRESS, 32)
                    if live_hook != hook_patched:
                        raise RuntimeError(
                            f"render hook changed unexpectedly: 0x{live_hook:08X}"
                        )
                    render_status = struct.unpack(
                        "<5I",
                        client.read_block(
                            allocated + STATUS_OFFSET, 24, progress=False
                        )[:20],
                    )
                    next_heartbeat = now + 0.25
                time.sleep(0.01)
        render_status = struct.unpack(
            "<5I",
            client.read_block(
                allocated + STATUS_OFFSET, 24, progress=False
            )[:20],
        )
        print(
            "render-status "
            f"executions={render_status[0]} "
            f"transform=0x{render_status[1]:08X} "
            f"primitive=0x{render_status[2]:08X} "
            f"end=0x{render_status[3]:08X} "
            f"original_target=0x{render_status[4]:08X}"
        )
    except KeyboardInterrupt:
        pass
    finally:
        if freecam is not None:
            freecam.close()
        if installed:
            try:
                client.write(HOOK_ADDRESS, HOOK_ORIGINAL, 32)
                print("original render hook restored")
            except Exception as error:
                print(f"WARNING: could not restore hook: {error}")
        if allocated:
            try:
                run_heap_bootstrap(client, allocated, freeing=True)
                print(f"released EE heap block 0x{allocated:08X}")
            except Exception as error:
                print(f"WARNING: could not release EE heap block: {error}")
        client.close()


if __name__ == "__main__":
    main()
