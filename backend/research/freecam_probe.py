#!/usr/bin/env python3
"""PINE free camera for The Warriors (SLUS-21215).

The active logical-camera object is heap allocated and can change during
cutscenes, scripted locks, and combat shots. A stable game global points at
the current object. Every compatible camera subclass has an eight-float
transform at object + 0x10: position (x, y, z, 1), then quaternion
(x, y, z, w).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
from pathlib import Path
import socket
import struct
import threading
import time

from pine_client import PineClient
from player_noclip import (
    ENTITY_REGISTRY,
    MOVE_TRANSFORM_BASE,
    MOVE_TRANSFORM_STRIDE,
    plausible_ee_pointer,
    read_player_position,
)


CAMERA_OBJECT_POINTER = 0x005D9148
CAMERA_VTABLE = 0x00535D50
# Real Heavy Rep starts with this derived camera object while the mission's
# "StartLock" is active. All known logical-camera subclasses share the common
# DevCamera vtable tail and the transform at object + 0x10. The freecam accepts
# that inherited family and follows CAMERA_OBJECT_POINTER when objects change.
START_LOCK_CAMERA_VTABLE = 0x005362D0
CAMERA_TRANSFORM_OFFSET = 0x10
CAMERA_VTABLE_SIGNATURE_OFFSET = 0x10
CAMERA_VTABLE_SIGNATURE_SIZE = 0x50
# Kept for older analysis scripts.  Runtime tools must call
# resolve_camera_transform() instead of relying on this old-session address.
CAMERA_TRANSFORM = 0x00CC5010
GRAPHICS_DEVICE_GLOBAL = 0x0050CDB4
GRAPHICS_VIEW0_OFFSET = 0x70
GRAPHICS_VIEW_CAMERA_OFFSET = 0x04
RW_CAMERA_MATRIX_OFFSET = 0x20
CAMERA_OVERRIDE_STUB_OFFSET = 0xEB0
CAMERA_OVERRIDE_MATRIX_OFFSET = 0xF20
CAMERA_OVERRIDE_FLAG_OFFSET = 0xF60
CAMERA_OVERRIDE_CAMERA_POINTER_OFFSET = 0xF64
CAMERA_BEGIN_UPDATE_HOOK = 0x00198948
CAMERA_BEGIN_UPDATE_ORIGINAL = 0x0C1209EE
MAIN_VIEW_HOOK_ADDRESS = 0x00185BFC
MAIN_VIEW_HOOK_ORIGINAL = 0x0C11C754
WORLD_TRANSITION_EXIT_CODE = 75
KIND_SLOT_COUNT = 10
KIND_VISIBILITY_MAGIC = b"TWKV"
DEBUG_STATE_MAGIC = b"TWDB"
DEBUG_STATE_VERSION = 1
DEBUG_STATE_HEADER = struct.Struct("<4sBBBBHH4fH")
DEBUG_SHAPE_RECORD = struct.Struct("<B3x7f")
POSITION_STATE_MAGIC = b"TWPS"
POSITION_STATE_VERSION = 1
POSITION_STATE_HEADER = struct.Struct("<4sBB2x3f")
VOLUME_SELECTION_MAGIC = b"TWVS"
VOLUME_SELECTION_VERSION = 1
VOLUME_SELECTION_HEADER = struct.Struct("<4sBBHH")
VOLUME_SELECTION_ALL = 0
VOLUME_SELECTION_MAX = 0xFF
# HuSetSlowMo writes the effective native update delta here. Rendering and the
# external freecam continue even when this value is reduced to nearly zero.
WORLD_TIMESTEP_ADDRESS = 0x005102CC
CAMERA_STABLE_SECONDS = 0.0
COLLISION_FILTER_KEYS = {
    1: "walls",
    2: "all",
    3: "floors",
    4: "walkable",
    5: "steep",
    6: "ceilings",
    7: "query-0x200-excluded",
    8: "solid-one-sided",
    9: "solid-two-sided",
}
COLLISION_FILTER_CHOICES = tuple(
    dict.fromkeys((*COLLISION_FILTER_KEYS.values(), "human-ignored"))
)

HUMAN_VTABLE = 0x0053F088
HUMAN_REGISTRY_ENTRIES = 0x4000
HUMAN_WATCH_RECORD = struct.Struct("<IH")


_camera_vtable_compatibility: dict[int, bool] = {
    CAMERA_VTABLE: True,
    START_LOCK_CAMERA_VTABLE: True,
}
_camera_vtable_signature: bytes | None = None


def camera_vtable_is_compatible(client: PineClient, vtable: int) -> bool:
    """Return whether a vtable belongs to the inherited logical-camera family."""
    global _camera_vtable_signature

    cached = _camera_vtable_compatibility.get(vtable)
    if cached is not None:
        return cached
    if not 0x00100000 <= vtable < 0x00700000:
        _camera_vtable_compatibility[vtable] = False
        return False
    if _camera_vtable_signature is None:
        _camera_vtable_signature = client.read_block(
            CAMERA_VTABLE + CAMERA_VTABLE_SIGNATURE_OFFSET,
            CAMERA_VTABLE_SIGNATURE_SIZE,
            progress=False,
        )
    candidate = client.read_block(
        vtable + CAMERA_VTABLE_SIGNATURE_OFFSET,
        CAMERA_VTABLE_SIGNATURE_SIZE,
        progress=False,
    )
    compatible = candidate == _camera_vtable_signature
    _camera_vtable_compatibility[vtable] = compatible
    return compatible


def resolve_camera_transform(
    client: PineClient,
    camera_object: int | None = None,
) -> int:
    if camera_object is None:
        camera_object = client.read(CAMERA_OBJECT_POINTER, 32)
    if not 0x00700000 <= camera_object < 0x01FF0000:
        raise RuntimeError(f"invalid active camera pointer: 0x{camera_object:08X}")
    vtable = client.read(camera_object, 32)
    if not camera_vtable_is_compatible(client, vtable):
        raise RuntimeError(
            f"active object is not in the logical-camera family at "
            f"0x{camera_object:08X}: "
            f"0x{vtable:08X}"
        )
    return camera_object + CAMERA_TRANSFORM_OFFSET


def wait_for_camera_transform(
    client: PineClient,
    timeout: float = 0.0,
    poll_interval: float = 0.1,
    stable_duration: float = CAMERA_STABLE_SECONDS,
) -> int:
    """Wait read-only until one compatible logical camera is active."""
    if timeout < 0 or stable_duration < 0:
        raise ValueError(
            "camera wait timeout and stable duration cannot be negative"
        )
    if poll_interval <= 0:
        raise ValueError("camera poll interval must be positive")

    deadline = time.monotonic() + timeout if timeout else math.inf
    candidate_transform: int | None = None
    candidate_since = 0.0
    last_notice = ""
    while True:
        now = time.monotonic()
        try:
            transform = resolve_camera_transform(client)
            if transform != candidate_transform:
                candidate_transform = transform
                candidate_since = now
            if now - candidate_since >= stable_duration:
                print(
                    f"Camera ready after {stable_duration:g}s stability at "
                    f"0x{transform:08X}.",
                    flush=True,
                )
                return transform
        except RuntimeError as error:
            candidate_transform = None
            candidate_since = 0.0
            notice = str(error)
            if notice != last_notice:
                print(
                    "No compatible mission camera is active yet. "
                    "Waiting without changing EE memory...",
                    flush=True,
                )
                last_notice = notice
        if now >= deadline:
            raise TimeoutError(
                f"gameplay camera did not become ready within {timeout:g} seconds"
            )
        time.sleep(poll_interval)

VK = {
    "W": 0x57,
    "A": 0x41,
    "S": 0x53,
    "D": 0x44,
    "Q": 0x51,
    "E": 0x45,
    "U": 0x55,
    "J": 0x4A,
    "H": 0x48,
    "K": 0x4B,
    "I": 0x49,
    "Y": 0x59,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "G": 0x47,
    "T": 0x54,
    "B": 0x42,
    "F": 0x46,
    "P": 0x50,
    "V": 0x56,
    "C": 0x43,
    "M": 0x4D,
    "N": 0x4E,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F10": 0x79,
}

_input_window = 0


class Point(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))


class Rect(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


def set_input_window(hwnd: int) -> None:
    """Limit global key polling to the active game window."""
    global _input_window
    _input_window = hwnd


def game_window_has_focus(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    foreground = int(user32.GetForegroundWindow() or 0)
    return bool(hwnd and foreground == hwnd)


_controller_input = None


def keyboard_pressed(key: str) -> bool:
    if not game_window_has_focus(_input_window):
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK[key]) & 0x8000)


def pressed(key: str) -> bool:
    if _controller_input is not None:
        if key in _controller_input.pulses:
            return True
        if _controller_input.controller_mode and key in {
            'W', 'A', 'S', 'D', 'Q', 'E', 'H', 'K', 'U', 'J', 'SHIFT', 'CTRL'
        }:
            return False
    return keyboard_pressed(key)


def watch_f10(
    requested: threading.Event,
    shutdown: threading.Event,
    poll_seconds: float = 0.01,
) -> None:
    """Latch F10 outside the PINE-heavy main loop so short taps are not lost."""
    while not shutdown.wait(poll_seconds):
        from session_guard import parent_alive
        if pressed("F10") or not parent_alive():
            requested.set()
            return


def kind_key_pressed(kind: int) -> bool:
    """Accept both the number row and the numeric keypad."""
    if not game_window_has_focus(_input_window):
        return False
    user32 = ctypes.windll.user32
    return bool(
        user32.GetAsyncKeyState(0x30 + kind) & 0x8000
        or user32.GetAsyncKeyState(0x60 + kind) & 0x8000
    )


def combined_mode_key_pressed() -> bool:
    """Accept apostrophe/right-of-zero OEM mappings used by common layouts."""
    if not game_window_has_focus(_input_window):
        return False
    user32 = ctypes.windll.user32
    return any(
        user32.GetAsyncKeyState(virtual_key) & 0x8000
        for virtual_key in (0xDE, 0xDB, 0xBD, 0xDD)
    )


class WorldTimeController:
    """Temporarily reduce the native world timestep while rendering continues."""

    def __init__(self, client: PineClient, scale: float) -> None:
        if not math.isfinite(scale) or not 0.0 <= scale < 1.0:
            raise ValueError("world time scale must be finite and in [0, 1)")
        self.client = client
        self.scale = scale
        self.original_bits: int | None = None
        self.target_bits: int | None = None

    @staticmethod
    def bits_to_float(bits: int) -> float:
        return struct.unpack("<f", struct.pack("<I", bits))[0]

    @staticmethod
    def float_to_bits(value: float) -> int:
        return struct.unpack("<I", struct.pack("<f", value))[0]

    @property
    def active(self) -> bool:
        return self.original_bits is not None

    def enable(self, original_bits: int | None = None) -> tuple[float, float]:
        if self.active:
            assert self.original_bits is not None
            assert self.target_bits is not None
            return (
                self.bits_to_float(self.original_bits),
                self.bits_to_float(self.target_bits),
            )
        if original_bits is None:
            original_bits = self.client.read(WORLD_TIMESTEP_ADDRESS, 32)
        original = self.bits_to_float(original_bits)
        if not math.isfinite(original) or original <= 0.0:
            raise RuntimeError(
                "native world timestep is not a finite positive value: "
                f"0x{original_bits:08X}"
            )
        target = original * self.scale
        target_bits = self.float_to_bits(target)
        self.client.write(WORLD_TIMESTEP_ADDRESS, target_bits, 32)
        if self.client.read(WORLD_TIMESTEP_ADDRESS, 32) != target_bits:
            raise RuntimeError("world timestep slowdown did not verify")
        self.original_bits = original_bits
        self.target_bits = target_bits
        return original, target

    def enforce(self) -> None:
        if not self.active:
            return
        assert self.target_bits is not None
        if self.client.read(WORLD_TIMESTEP_ADDRESS, 32) != self.target_bits:
            self.client.write(WORLD_TIMESTEP_ADDRESS, self.target_bits, 32)

    def disable(self) -> float | None:
        if not self.active:
            return None
        assert self.original_bits is not None
        original_bits = self.original_bits
        self.client.write(WORLD_TIMESTEP_ADDRESS, original_bits, 32)
        if self.client.read(WORLD_TIMESTEP_ADDRESS, 32) != original_bits:
            raise RuntimeError("world timestep restoration did not verify")
        self.original_bits = None
        self.target_bits = None
        return self.bits_to_float(original_bits)


def rotate(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> list[float]:
    """Rotate vector v by quaternion q=(x,y,z,w)."""
    x, y, z, w = q
    vx, vy, vz = v
    # q * v * conjugate(q), expanded to avoid temporary quaternion objects.
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return [
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    ]


def normalize(v: list[float]) -> list[float]:
    length = math.sqrt(sum(component * component for component in v))
    return [component / length for component in v] if length else v


class HumanSpawnWatcher:
    """Log the first sampled frame of named humans through the active PINE link."""

    def __init__(self, prefix: str, hz: float) -> None:
        self.prefix = prefix
        self.interval = 1.0 / hz
        self.next_poll = 0.0
        self.live: set[tuple[int, str]] = set()

    @staticmethod
    def _in_frustum(
        actor: tuple[float, float, float],
        camera: list[float],
        quaternion: tuple[float, float, float, float],
        frustum_slopes: tuple[float, float],
    ) -> tuple[bool, float, float, float]:
        # Test the actor's torso rather than its ground-level origin. This is a
        # geometric frustum test only; level geometry may still occlude it.
        relative = (
            actor[0] - camera[0],
            actor[1] - camera[1],
            actor[2] + 1.0 - camera[2],
        )
        right = normalize(rotate(quaternion, (1.0, 0.0, 0.0)))
        up = normalize(rotate(quaternion, (0.0, 0.0, 1.0)))
        forward = normalize(rotate(quaternion, (0.0, 1.0, 0.0)))
        lateral = sum(relative[index] * right[index] for index in range(3))
        vertical = sum(relative[index] * up[index] for index in range(3))
        depth = sum(relative[index] * forward[index] for index in range(3))
        horizontal_slope, vertical_slope = frustum_slopes
        visible = (
            depth > 0.0
            and abs(lateral) <= depth * abs(horizontal_slope)
            and abs(vertical) <= depth * abs(vertical_slope)
        )
        return visible, depth, lateral, vertical

    def poll(
        self,
        client: PineClient,
        now: float,
        camera: list[float],
        quaternion: tuple[float, float, float, float],
        frustum_slopes: tuple[float, float],
    ) -> int:
        if now < self.next_poll:
            return len(self.live)
        self.next_poll = now + self.interval
        registry = client.read_block(
            ENTITY_REGISTRY,
            HUMAN_REGISTRY_ENTRIES * 8,
            progress=False,
        )
        candidates: list[tuple[int, int, int]] = []
        seen_pointers: set[int] = set()
        for index in range(HUMAN_REGISTRY_ENTRIES):
            human, generation = HUMAN_WATCH_RECORD.unpack_from(
                registry, index * 8
            )
            if human in seen_pointers or not plausible_ee_pointer(human):
                continue
            seen_pointers.add(human)
            candidates.append((human, index, generation))

        ranges: list[tuple[int, int]] = []
        for human, _index, _generation in candidates:
            # Human pool objects are four-byte aligned, while the PINE block
            # helper requires eight-byte aligned ranges.
            ranges.extend(((human & ~7, 16), ((human + 0x80) & ~7, 32)))
        blocks = client.read_blocks(ranges)
        current: set[tuple[int, str]] = set()
        for candidate_index, (human, index, generation) in enumerate(candidates):
            header = blocks[candidate_index * 2]
            identity = blocks[candidate_index * 2 + 1]
            header_offset = human & 7
            identity_offset = (human + 0x80) & 7
            if struct.unpack_from("<I", header, header_offset)[0] != HUMAN_VTABLE:
                continue
            name = identity[identity_offset : identity_offset + 16].split(
                b"\0", 1
            )[0].decode(
                "ascii", errors="replace"
            )
            if not name.startswith(self.prefix):
                continue
            move_index = struct.unpack_from(
                "<h", identity, identity_offset + 0x12
            )[0]
            if 0 <= move_index < 0x4000:
                position_address = (
                    MOVE_TRANSFORM_BASE + move_index * MOVE_TRANSFORM_STRIDE
                )
            else:
                position_address = human + 0x2C0
            position_raw = client.read_block(
                position_address & ~7, 24, progress=False
            )
            position_offset = position_address & 7
            actor = struct.unpack_from("<3f", position_raw, position_offset)
            key = (human, name)
            current.add(key)
            if key in self.live:
                continue
            in_frustum, depth, lateral, vertical = self._in_frustum(
                actor, camera, quaternion, frustum_slopes
            )
            handle = (index << 16) | generation
            print(
                "HUMAN_WATCH_APPEARED "
                f"name={name!r} handle=0x{handle:08X} "
                f"human=0x{human:08X} "
                f"actor=({actor[0]:.4f},{actor[1]:.4f},{actor[2]:.4f}) "
                f"camera=({camera[0]:.4f},{camera[1]:.4f},{camera[2]:.4f}) "
                f"quaternion=({quaternion[0]:.6f},{quaternion[1]:.6f},"
                f"{quaternion[2]:.6f},{quaternion[3]:.6f}) "
                f"frustum=({frustum_slopes[0]:.6f},{frustum_slopes[1]:.6f}) "
                f"in_frustum={in_frustum} depth={depth:.4f} "
                f"lateral={lateral:.4f} vertical={vertical:.4f}",
                flush=True,
            )
        for human, name in self.live - current:
            print(
                "HUMAN_WATCH_DISAPPEARED "
                f"name={name!r} human=0x{human:08X}",
                flush=True,
            )
        self.live = current
        return len(current)


def solve_linear_system(matrix: list[list[float]], values: list[float]) -> list[float]:
    """Solve a small dense linear system with partial pivoting."""
    augmented = [row.copy() + [value] for row, value in zip(matrix, values)]
    size = len(values)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1.0e-9:
            raise ValueError("camera-space samples do not span all three axes")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [component / scale for component in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(size + 1)
            ]
    return [augmented[row][-1] for row in range(size)]


def render_camera_world_position_from_matrix(
    raw: bytes,
) -> tuple[float, float, float]:
    """Invert one raw RenderWare world-to-camera affine matrix."""
    if len(raw) != 0x40:
        raise ValueError("RenderWare camera matrix must contain exactly 64 bytes")
    values = struct.unpack("<16f", raw)
    a, b, c = values[0:3]
    d, e, f = values[4:7]
    g, h, i = values[8:11]
    px, py, pz = values[12:15]
    determinant = (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
    )
    if abs(determinant) < 1.0e-9:
        raise RuntimeError("RenderWare camera matrix is singular")
    inverse = (
        (
            (e * i - f * h) / determinant,
            (c * h - b * i) / determinant,
            (b * f - c * e) / determinant,
        ),
        (
            (f * g - d * i) / determinant,
            (a * i - c * g) / determinant,
            (c * d - a * f) / determinant,
        ),
        (
            (d * h - e * g) / determinant,
            (b * g - a * h) / determinant,
            (a * e - b * d) / determinant,
        ),
    )
    translation = (px, py, pz)
    return tuple(
        -sum(translation[row] * inverse[row][axis] for row in range(3))
        for axis in range(3)
    )  # type: ignore[return-value]


def render_camera_world_position(
    client: PineClient, render_camera: int
) -> tuple[float, float, float]:
    """Read and invert the RenderWare world-to-camera affine matrix."""
    raw = client.read_block(
        render_camera + RW_CAMERA_MATRIX_OFFSET, 0x40, progress=False
    )
    return render_camera_world_position_from_matrix(raw)


def fit_affine_space(
    samples: list[tuple[list[float], tuple[float, float, float]]]
) -> tuple[list[list[float]], float]:
    """Fit render_xyz = rows * (game_xyz, 1) and return RMS error."""
    normal = [[0.0] * 4 for _ in range(4)]
    targets = [[0.0] * 4 for _ in range(3)]
    for game_position, render_position in samples:
        row = [*game_position, 1.0]
        for left in range(4):
            for right in range(4):
                normal[left][right] += row[left] * row[right]
            for axis in range(3):
                targets[axis][left] += row[left] * render_position[axis]
    coefficients = [
        solve_linear_system(normal, targets[axis]) for axis in range(3)
    ]
    squared_error = 0.0
    for game_position, render_position in samples:
        row = [*game_position, 1.0]
        for axis in range(3):
            predicted = sum(
                coefficients[axis][index] * row[index] for index in range(4)
            )
            squared_error += (predicted - render_position[axis]) ** 2
    rms = math.sqrt(squared_error / (len(samples) * 3))
    return coefficients, rms


def quaternion_multiply(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_axis_angle(
    axis: tuple[float, float, float], angle: float
) -> tuple[float, float, float, float]:
    half = angle * 0.5
    sine = math.sin(half)
    return axis[0] * sine, axis[1] * sine, axis[2] * sine, math.cos(half)


def normalize_quaternion(
    q: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(component * component for component in q))
    return tuple(component / length for component in q)  # type: ignore[return-value]


def build_freecam_render_matrix(
    position: list[float] | tuple[float, float, float],
    quaternion: tuple[float, float, float, float],
    frustum_slopes: tuple[float, float],
) -> bytes:
    """Build the exact RenderWare world-to-camera matrix used by the game."""
    horizontal_slope, vertical_slope = frustum_slopes
    if abs(horizontal_slope) < 1.0e-6 or abs(vertical_slope) < 1.0e-6:
        raise ValueError("camera frustum slopes cannot be zero")

    def render_vector(
        game_vector: list[float],
    ) -> tuple[float, float, float]:
        return game_vector[0], game_vector[2], -game_vector[1]

    right = render_vector(rotate(quaternion, (1.0, 0.0, 0.0)))
    up = render_vector(rotate(quaternion, (0.0, 0.0, 1.0)))
    forward = render_vector(rotate(quaternion, (0.0, 1.0, 0.0)))
    basis = [
        [right[row], up[row], forward[row]]
        for row in range(3)
    ]
    projection = (
        (0.5 / horizontal_slope, 0.0, 0.0),
        (0.0, -0.5 / vertical_slope, 0.0),
        (0.5, 0.5, 1.0),
    )
    matrix = [
        [
            sum(basis[row][axis] * projection[axis][column] for axis in range(3))
            for column in range(3)
        ]
        for row in range(3)
    ]
    render_position = (position[0], position[2], -position[1])
    translation = [
        -sum(render_position[row] * matrix[row][column] for row in range(3))
        for column in range(3)
    ]
    return struct.pack(
        "<16f",
        *matrix[0],
        0.0,
        *matrix[1],
        0.0,
        *matrix[2],
        0.0,
        *translation,
        0.0,
    )


def mips_jal(address: int) -> int:
    return (0x03 << 26) | ((address >> 2) & 0x03FFFFFF)


def find_game_window() -> int:
    ctypes.windll.user32.FindWindowW.restype = ctypes.c_void_p
    return int(ctypes.windll.user32.FindWindowW(None, "The Warriors") or 0)


def window_center(hwnd: int) -> tuple[int, int] | None:
    rect = Rect()
    if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--hz", type=float, default=120.0)
    parser.add_argument("--native-camera-code-address", type=lambda v: int(v, 0), default=0)
    parser.add_argument("--speed", type=float, default=8.0)
    parser.add_argument("--look-speed", type=float, default=90.0)
    parser.add_argument("--mouse-sensitivity", type=float, default=0.14)
    parser.add_argument(
        "--camera-stable-seconds",
        type=float,
        default=CAMERA_STABLE_SECONDS,
        help="continuous compatible-camera time required before the first EE write",
    )
    parser.add_argument(
        "--auto",
        choices=("none", "right", "forward", "up"),
        default="none",
        help="apply continuous movement without relying on keyboard input",
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument(
        "--human-watch-prefix",
        default="",
        help="log the first sampled frame of live humans whose name starts here",
    )
    parser.add_argument(
        "--human-watch-hz",
        type=float,
        default=10.0,
        help="entity-registry sample rate for --human-watch-prefix",
    )
    parser.add_argument(
        "--human-watch-slow-after",
        type=int,
        default=0,
        help="engage the configured world slowdown after this many matches",
    )
    parser.add_argument(
        "--broadcast-port",
        type=int,
        default=0,
        help="send the current 32-byte transform to 127.0.0.1 on this UDP port",
    )
    parser.add_argument(
        "--gang-broadcast-port",
        type=int,
        default=0,
        help="send gang/NPC/spawner snapshots to this separate UDP port",
    )
    parser.add_argument(
        "--gang-telemetry-hz",
        type=float,
        default=5.0,
        help="sample rate for --gang-broadcast-port",
    )
    parser.add_argument(
        "--destroyed-cp4-telemetry-hz",
        type=float,
        default=0.0,
        help=(
            "append Destroyed CP4 counter/gang telemetry to the UDP feed "
            "at this rate; zero disables it"
        ),
    )
    parser.add_argument(
        "--destroyed-cp4-ai-freeze-hotkeys",
        action="store_true",
        help=(
            "enable reversible F6 brain freeze on all three CP4 gangs and "
            "F7 on all except the two OOB ClubDestLieut actors"
        ),
    )
    parser.add_argument(
        "--kind-visibility-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE address of the renderer's ten kind visibility bytes",
    )
    parser.add_argument(
        "--collision-data-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE address of a collision renderer compact-data block",
    )
    parser.add_argument("--collision-radius", type=float, default=35.0)
    parser.add_argument("--collision-limit", type=int, default=80)
    parser.add_argument(
        "--collision-orientation",
        choices=COLLISION_FILTER_CHOICES,
        default="walls",
    )
    parser.add_argument("--collision-wall-normal-z", type=float, default=0.35)
    parser.add_argument("--collision-update-hz", type=float, default=8.0)
    parser.add_argument(
        "--combined-code-address",
        type=lambda value: int(value, 0),
        default=0,
        help="shared EE code block for trigger/collision mode switching",
    )
    parser.add_argument(
        "--combined-data-address",
        type=lambda value: int(value, 0),
        default=0,
        help="shared EE data block for trigger/collision mode switching",
    )
    parser.add_argument(
        "--combined-volumes",
        default="",
        help="trigger-volume JSON used by combined renderer mode",
    )
    parser.add_argument(
        "--simultaneous-code-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE code block used by the simultaneous trigger/terrain renderer",
    )
    parser.add_argument(
        "--simultaneous-data-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE trigger-data block used by the simultaneous renderer",
    )
    parser.add_argument(
        "--flag-render-state-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE TWFG header controlling native trigger/all/off flag lines",
    )
    parser.add_argument(
        "--volume-selection-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE byte controlling all volumes (0) or one-based renderer index",
    )
    parser.add_argument(
        "--volume-count",
        type=int,
        default=0,
        help="number of selectable native trigger volumes",
    )
    parser.add_argument(
        "--camera-override-code-address",
        type=lambda value: int(value, 0),
        default=0,
        help="renderer code block containing the native final-camera override",
    )
    parser.add_argument(
        "--lifecycle-renderer-code-address",
        type=lambda value: int(value, 0),
        default=0,
        help="renderer block whose hooks must be disarmed during world loads",
    )
    parser.add_argument(
        "--world-id",
        default="",
        help="catalog id used to decide whether a saved camera position is reusable",
    )
    parser.add_argument(
        "--transition-state",
        type=Path,
        help="write persistent controls here before requesting a world rebuild",
    )
    parser.add_argument(
        "--resume-state",
        type=Path,
        help="restore persistent controls from a previous world session",
    )
    parser.add_argument(
        "--ready-file",
        type=Path,
        help="write a startup handshake after collision/player initialization",
    )
    parser.add_argument(
        "--transition-stable-seconds",
        type=float,
        default=1.0,
        help="coherent new-world time required before handing off to the supervisor",
    )
    parser.add_argument(
        "--trace-render-space",
        action="store_true",
        help="sample RenderWare camera position and fit an affine game-to-render map",
    )
    parser.add_argument(
        "--trace-camera-race",
        action="store_true",
        help=(
            "diagnose game writes that race the freecam and large RenderWare "
            "camera jumps"
        ),
    )
    parser.add_argument(
        "--trace-loop-timing",
        action="store_true",
        help="report freecam loop gaps and periodic collision-update timings",
    )
    parser.add_argument(
        "--carry-player",
        action="store_true",
        help="carry the live player at a camera-relative noclip offset",
    )
    parser.add_argument(
        "--start-player-released",
        action="store_true",
        help="enable F8 player carry controls but start with the player released",
    )
    parser.add_argument(
        "--player-offset-forward",
        type=float,
        default=3.0,
        help="player distance along the freecam forward vector",
    )
    parser.add_argument(
        "--player-offset-up",
        type=float,
        default=-1.5,
        help="player vertical offset from the freecam",
    )
    parser.add_argument(
        "--player-update-hz",
        type=float,
        default=60.0,
        help="maximum player transform update rate while attached",
    )
    parser.add_argument(
        "--player-safety",
        action="store_true",
        help="temporarily enable the native god/no-target bits",
    )
    parser.add_argument(
        "--player-god-mode",
        action="store_true",
        help="manage native god mode independently; initially enabled",
    )
    parser.add_argument(
        "--player-no-target",
        action="store_true",
        help="manage no-target and enable it only while noclip is attached",
    )
    parser.add_argument(
        "--player-air-stand-toggle",
        action="store_true",
        help="enable Y player-only fall-entry blocking",
    )
    parser.add_argument(
        "--camera-mode-toggle",
        action="store_true",
        help="enable V freecam/normal-camera switching",
    )
    parser.add_argument(
        "--player-debug-toggle",
        action="store_true",
        help="enable T player collision/body-data broadcasting",
    )
    parser.add_argument(
        "--object-debug-toggle",
        action="store_true",
        help="enable B nearby live type-1 object OBB visualization",
    )
    parser.add_argument(
        "--climb-object-support",
        action="store_true",
        help=(
            "include OBBs from confirmed dynamic climb classes "
            "(currently Car) in C diagnostics"
        ),
    )
    parser.add_argument(
        "--world-time-toggle",
        action="store_true",
        help="enable P native world slowdown/freezing while rendering continues",
    )
    parser.add_argument(
        "--disable-world-time-key",
        action="store_true",
        help="create world-time control without accepting the global P hotkey",
    )
    parser.add_argument(
        "--world-time-scale",
        type=float,
        default=0.0001,
        help="world speed while P slowdown is active (default: 0.0001)",
    )
    parser.add_argument(
        "--start-world-time-slowed",
        action="store_true",
        help="adopt a launcher-prearmed world slowdown at startup",
    )
    parser.add_argument(
        "--world-time-original-bits",
        type=lambda value: int(value, 0),
        default=0,
        help="original native timestep bits for a prearmed slowdown",
    )
    parser.add_argument(
        "--keep-player-gravity",
        action="store_true",
        help="do not clear accumulated vertical speed while the player is attached",
    )
    parser.add_argument(
        "--restore-player-on-exit",
        action="store_true",
        help="restore the starting player position when the probe ends",
    )
    parser.add_argument("--ui-panels-state", type=Path)
    parser.add_argument("--controller", action="store_true")
    parser.add_argument("--capture-keyboard-pad", action="store_true")
    parser.add_argument("--return-to-follow-camera", action="store_true")
    parser.add_argument("--control-state", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--profile", choices=('freecam', 'maptriggers'), default='maptriggers')
    args = parser.parse_args()
    if args.human_watch_hz <= 0.0:
        raise ValueError("human watch rate must be positive")
    if args.human_watch_slow_after < 0:
        raise ValueError("human watch slowdown count cannot be negative")
    if args.human_watch_slow_after and not args.world_time_toggle:
        raise ValueError("human watch slowdown requires --world-time-toggle")
    if args.kind_visibility_address and not (
        0x00700000
        <= args.kind_visibility_address
        <= 0x01FFFFFF - KIND_SLOT_COUNT
    ):
        raise ValueError("kind visibility address is outside EE user memory")
    if args.collision_data_address and not (
        0x00700000 <= args.collision_data_address <= 0x01FFFFFF - 0x1000
    ):
        raise ValueError("collision data address is outside EE user memory")
    combined_requested = bool(
        args.combined_code_address or args.combined_data_address
    )
    simultaneous_requested = bool(
        args.simultaneous_code_address
        or args.simultaneous_data_address
    )
    volume_selection_requested = bool(
        args.volume_selection_address or args.volume_count
    )
    if combined_requested and not (
        args.combined_code_address and args.combined_data_address
    ):
        raise ValueError("combined mode requires both code and data addresses")
    for label, address in (
        ("combined code", args.combined_code_address),
        ("combined data", args.combined_data_address),
    ):
        if address and not 0x00700000 <= address <= 0x01FFFFFF - 0x1000:
            raise ValueError(f"{label} address is outside EE user memory")
    if combined_requested and not args.combined_volumes:
        raise ValueError("combined mode requires a trigger-volume JSON")
    if combined_requested and not args.kind_visibility_address:
        raise ValueError("combined mode requires the trigger visibility address")
    if simultaneous_requested and not (
        args.simultaneous_code_address
        and args.simultaneous_data_address
        and args.kind_visibility_address
        and args.flag_render_state_address
    ):
        raise ValueError(
            "simultaneous mode requires code, data, kind visibility, "
            "and flag-render-state addresses"
        )
    if volume_selection_requested and not (
        args.volume_selection_address and args.volume_count
    ):
        raise ValueError(
            "single-volume selection requires its address and volume count"
        )
    if volume_selection_requested and not simultaneous_requested:
        raise ValueError(
            "single-volume selection requires the simultaneous renderer"
        )
    if args.volume_selection_address and not (
        0x00700000 <= args.volume_selection_address <= 0x01FFFFFF
    ):
        raise ValueError("volume selection address is outside EE user memory")
    if not 0 <= args.volume_count <= VOLUME_SELECTION_MAX:
        raise ValueError("volume count must fit the native selection byte")
    if combined_requested and simultaneous_requested:
        raise ValueError(
            "combined switching and simultaneous rendering are mutually exclusive"
        )
    for label, address in (
        ("simultaneous code", args.simultaneous_code_address),
        ("simultaneous data", args.simultaneous_data_address),
        ("flag render state", args.flag_render_state_address),
        ("camera override code", args.camera_override_code_address),
        ("lifecycle renderer code", args.lifecycle_renderer_code_address),
    ):
        if address and not 0x00700000 <= address <= 0x01FFFFFF - 0x1000:
            raise ValueError(f"{label} address is outside EE user memory")
    if (
        simultaneous_requested
        and args.camera_override_code_address
        and args.camera_override_code_address != args.simultaneous_code_address
    ):
        raise ValueError(
            "camera override and simultaneous renderer must use the same code block"
        )
    if (
        args.kind_visibility_address
        and args.collision_data_address
        and not combined_requested
    ):
        raise ValueError(
            "kind visibility and collision updating cannot share one freecam"
        )
    if args.collision_radius < 0:
        raise ValueError("collision radius cannot be negative")
    collision_limit_max = 64 if simultaneous_requested else 101
    if not 1 <= args.collision_limit <= collision_limit_max:
        raise ValueError(
            f"collision limit must be between 1 and {collision_limit_max}"
        )
    if not 0 <= args.collision_wall_normal_z <= 1:
        raise ValueError("collision wall-normal-z must be between zero and one")
    if args.collision_update_hz <= 0:
        raise ValueError("collision update Hz must be positive")
    if args.camera_stable_seconds < 0:
        raise ValueError("camera stable seconds cannot be negative")
    if args.transition_stable_seconds < 0:
        raise ValueError("transition stable seconds cannot be negative")
    if args.player_update_hz <= 0:
        raise ValueError("player update Hz must be positive")
    if not 0.0 <= args.destroyed_cp4_telemetry_hz <= 30.0:
        raise ValueError("Destroyed CP4 telemetry Hz must be in [0, 30]")
    if args.destroyed_cp4_telemetry_hz and not args.broadcast_port:
        raise ValueError("Destroyed CP4 telemetry requires --broadcast-port")
    if not 0.5 <= args.gang_telemetry_hz <= 20.0:
        raise ValueError("gang telemetry Hz must be in [0.5, 20]")
    if (
        args.destroyed_cp4_ai_freeze_hotkeys
        and not args.destroyed_cp4_telemetry_hz
    ):
        raise ValueError("Destroyed CP4 AI freeze hotkeys require telemetry")
    if not math.isfinite(args.world_time_scale) or not (
        0.0 <= args.world_time_scale < 1.0
    ):
        raise ValueError("world time scale must be finite and in [0, 1)")
    if not args.world_time_toggle and args.world_time_scale != 0.0001:
        raise ValueError("--world-time-scale requires --world-time-toggle")
    if args.start_world_time_slowed != bool(args.world_time_original_bits):
        raise ValueError(
            "prearmed world time requires both --start-world-time-slowed "
            "and --world-time-original-bits"
        )
    if args.start_world_time_slowed and not args.world_time_toggle:
        raise ValueError("prearmed world time requires --world-time-toggle")
    if not all(
        math.isfinite(value)
        for value in (args.player_offset_forward, args.player_offset_up)
    ):
        raise ValueError("player offsets must be finite")
    if args.start_player_released and not args.carry_player:
        raise ValueError("--start-player-released requires --carry-player")
    if (
        args.player_safety
        or args.player_god_mode
        or args.player_no_target
        or args.player_air_stand_toggle
        or args.camera_mode_toggle
        or args.player_debug_toggle
        or args.object_debug_toggle
        or args.keep_player_gravity
        or args.restore_player_on_exit
    ) and not args.carry_player:
        raise ValueError(
            "player/camera debug options require --carry-player"
        )
    if args.player_air_stand_toggle and not simultaneous_requested:
        raise ValueError(
            "player air stand requires the simultaneous renderer code block"
        )
    if args.object_debug_toggle and not simultaneous_requested:
        raise ValueError(
            "object OBB visualization requires the simultaneous renderer"
        )
    if args.climb_object_support and not simultaneous_requested:
        raise ValueError(
            "dynamic climb visualization requires the simultaneous renderer"
        )
    obb_layout_supported = bool(
        args.object_debug_toggle or args.climb_object_support
    )
    if args.native_camera_code_address and (
        not obb_layout_supported
        or args.native_camera_code_address != args.simultaneous_code_address
        or args.native_camera_code_address != args.lifecycle_renderer_code_address
        or args.camera_override_code_address
    ):
        raise ValueError(
            "Native freecam requires the same combined renderer/lifecycle block "
            "with external terrain records; incompatible with the old camera diagnostic"
        )

    resume_state: dict = {}
    if args.resume_state is not None and args.resume_state.is_file():
        try:
            loaded_state = json.loads(
                args.resume_state.read_text(encoding="utf-8")
            )
            if isinstance(loaded_state, dict):
                resume_state = loaded_state
        except (OSError, ValueError) as error:
            print(
                f"WARNING: could not load persistent freecam state: {error}",
                flush=True,
            )

    client = PineClient(port=args.port)
    broadcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if args.broadcast_port else None
    gang_broadcast = (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if args.gang_broadcast_port
        else None
    )
    gang_reader = None
    gang_snapshot_to_packet = None
    next_gang_telemetry_update = 0.0
    last_gang_telemetry_error = ""
    if gang_broadcast is not None:
        from all_gang_overlay import AllGangReader, snapshot_to_packet

        gang_reader = AllGangReader()
        gang_snapshot_to_packet = snapshot_to_packet
    next_broadcast_update = 0.0
    broadcast_player_human = 0
    broadcast_player_position: tuple[float, float, float] | None = None
    next_broadcast_player_update = 0.0
    next_broadcast_player_refresh = 0.0
    destroyed_cp4_sampler = None
    destroyed_cp4_packet = b""
    next_destroyed_cp4_update = 0.0
    destroyed_ai_controller = None
    destroyed_ai_mode = "off"
    next_destroyed_ai_enforce = 0.0
    destroyed_ai_all_was_down = False
    destroyed_ai_except_was_down = False
    if args.destroyed_cp4_telemetry_hz:
        from destroyed_cp4_telemetry import DestroyedCP4TelemetrySampler

        destroyed_cp4_sampler = DestroyedCP4TelemetrySampler(client)
        destroyed_cp4_packet = destroyed_cp4_sampler.sample(client)
    if args.destroyed_cp4_ai_freeze_hotkeys:
        from destroyed_cp4_ai_control import (
            MODES,
            DestroyedAIFreezeController,
        )

        assert destroyed_cp4_sampler is not None
        destroyed_ai_controller = DestroyedAIFreezeController()
        saved_ai_mode = resume_state.get("destroyed_ai_freeze_mode", "off")
        if saved_ai_mode not in MODES:
            saved_ai_mode = "off"
        destroyed_ai_mode = saved_ai_mode
        destroyed_ai_controller.set_mode(
            client,
            destroyed_cp4_sampler.latest_snapshot,
            destroyed_ai_mode,
        )
        destroyed_cp4_sampler.set_ai_freeze_mode(destroyed_ai_mode)
        destroyed_cp4_packet = destroyed_cp4_sampler.sample(client)
    camera_transform = wait_for_camera_transform(
        client, stable_duration=args.camera_stable_seconds
    )
    camera_object = camera_transform - CAMERA_TRANSFORM_OFFSET
    camera_available = True
    camera_switches = 0
    camera_recoveries = 0
    camera_resolution_failures = 0
    last_camera_resolution_error = ""
    next_camera_recovery = 0.0
    raw = client.read_block(camera_transform, 0x60)
    print(
        f"active_camera_object=0x{camera_object:08X} "
        f"transform=0x{camera_transform:08X}"
    )
    values = list(struct.unpack_from("<8f", raw))
    position = values[:3]
    start_position = position.copy()
    position_w = values[3]
    quaternion = normalize_quaternion(tuple(values[4:8]))
    frustum_slopes = struct.unpack_from("<2f", raw, 0x4C)
    resume_same_world = bool(
        args.world_id
        and resume_state.get("world_id") == args.world_id
    )
    saved_position = resume_state.get("position")
    saved_quaternion = resume_state.get("quaternion")
    if (
        resume_same_world
        and isinstance(saved_position, list)
        and len(saved_position) == 3
        and all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in saved_position
        )
        and isinstance(saved_quaternion, list)
        and len(saved_quaternion) == 4
        and all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in saved_quaternion
        )
    ):
        position = [float(value) for value in saved_position]
        quaternion = normalize_quaternion(
            tuple(float(value) for value in saved_quaternion)
        )
        print(
            f"restored same-world freecam transform for {args.world_id}",
            flush=True,
        )
    world_up = [0.0, 0.0, 1.0]
    game_window = find_game_window()
    set_input_window(game_window)
    original_cursor = Point()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(original_cursor))
    mouse_engaged = False
    render_camera = 0
    render_samples: list[tuple[list[float], tuple[float, float, float]]] = []
    next_render_sample = time.monotonic()
    last_written_position = position.copy()
    last_written_block = bytes(raw[:0x20])
    camera_race_start = time.monotonic()
    camera_race_reads = 0
    camera_race_logical_overrides = 0
    camera_race_max_logical_error = 0.0
    camera_race_render_reads = 0
    camera_race_render_jumps = 0
    camera_race_max_render_step = 0.0
    camera_race_last_render_position: tuple[float, float, float] | None = None
    camera_race_next_render_sample = camera_race_start
    camera_race_logical_reports = 0
    camera_race_render_reports = 0
    camera_override_enabled = False
    native_camera = None
    camera_override_begin_hook_installed = False
    camera_override_updates = 0
    next_camera_override_update = 0.0
    player_controller = None
    air_stand_controller = None
    panel_hotkeys = None
    visibility_hotkeys = None
    player_target: tuple[float, float, float] | None = None
    next_player_update = 0.0
    next_player_refresh = 0.0
    player_toggle_was_down = False
    air_stand_toggle_was_down = pressed("Y")
    god_toggle_was_down = False
    camera_toggle_was_down = False
    player_debug_toggle_was_down = False
    object_debug_toggle_was_down = False
    world_time_toggle_was_down = (
        pressed("P")
        if args.world_time_toggle and not args.disable_world_time_key
        else False
    )
    world_time = (
        WorldTimeController(client, args.world_time_scale)
        if args.world_time_toggle
        else None
    )
    if args.start_world_time_slowed:
        assert world_time is not None
        original, target = world_time.enable(
            args.world_time_original_bits
        )
        print(
            "world time: STARTED "
            + (
                "PAUSED"
                if args.world_time_scale == 0.0
                else (
                    "PRACTICALLY PAUSED "
                    f"({args.world_time_scale * 100:.4g}%)"
                )
            )
            + " "
            f"(timestep {original:.9g} -> {target:.9g})",
            flush=True,
        )
    next_world_time_enforce = 0.0
    freecam_enabled = bool(resume_state.get("freecam_enabled", True))
    global _controller_input
    controller_input = None
    keyboard_v_was_down = keyboard_pressed('V')
    if args.controller:
        from controller_input import ControllerInput
        controller_input = ControllerInput(client, args.simultaneous_code_address, args.capture_keyboard_pad, args.control_state)
        controller_input.controller_mode = bool(resume_state.get('controller_mode', False))
        controller_input.hud_toggle = int(resume_state.get('hud_toggle', 0))
        _controller_input = controller_input
    carry_preference = bool(
        resume_state.get(
            "carry_player",
            not args.start_player_released,
        )
    )
    air_stand_preference = bool(
        args.player_air_stand_toggle
        and resume_state.get("air_stand", False)
    )
    player_debug_visible = bool(
        resume_state.get("player_debug_visible", False)
    )
    object_debug_visible = bool(
        args.object_debug_toggle
        and resume_state.get("object_debug_visible", False)
    )
    player_debug_position = (0.0, 0.0, 0.0)
    player_debug_radius = 1.0
    player_debug_shapes = []
    player_debug_sampler = None
    next_player_debug_update = 0.0
    last_player_debug_signature = None
    object_debug_shapes = []
    next_object_debug_update = 0.0
    last_object_debug_signature = None
    last_obb_overlay_kind: str | None = None
    if (
        args.trace_render_space
        or args.trace_camera_race
        or args.camera_override_code_address
    ):
        graphics_device = client.read(GRAPHICS_DEVICE_GLOBAL, 32)
        if not 0x00700000 <= graphics_device < 0x01FF0000:
            raise RuntimeError(f"invalid graphics device: 0x{graphics_device:08X}")
        render_camera = client.read(
            graphics_device + GRAPHICS_VIEW0_OFFSET + GRAPHICS_VIEW_CAMERA_OFFSET,
            32,
        )
        if not 0x00700000 <= render_camera < 0x01FF0000:
            raise RuntimeError(f"invalid RenderWare camera: 0x{render_camera:08X}")
        print(f"render_camera=0x{render_camera:08X}")
    if args.camera_override_code_address:
        client.write(
            args.camera_override_code_address + CAMERA_OVERRIDE_FLAG_OFFSET,
            0,
            32,
        )
        client.write(
            args.camera_override_code_address
            + CAMERA_OVERRIDE_CAMERA_POINTER_OFFSET,
            render_camera,
            32,
        )
        print(
            "native_camera_override="
            f"0x{args.camera_override_code_address:08X}",
            flush=True,
        )

    deadline = (
        time.monotonic() + args.duration
        if args.duration > 0
        else math.inf
    )
    previous = time.monotonic()
    loop_timing_iterations = 0
    loop_timing_over_16ms = 0
    loop_timing_over_33ms = 0
    loop_timing_max_gap = 0.0
    collision_update_timings: list[float] = []
    human_watcher = (
        HumanSpawnWatcher(args.human_watch_prefix, args.human_watch_hz)
        if args.human_watch_prefix
        else None
    )
    human_watch_slowed = False
    writes = 0
    key_polls = {
        key: 0
        for key in (
            "W", "A", "S", "D", "Q", "E", "U", "J", "H", "K",
            "I", "G", "T", "B", "F", "P", "V", "C", "M", "N",
            "F6", "F7", "F8", "F10", "Y"
        )
    }
    key_polls.update({f"KIND_{kind}": 0 for kind in range(KIND_SLOT_COUNT)})
    key_polls["MODE_TOGGLE"] = 0
    saved_kind_visibility = resume_state.get("kind_visibility")
    kind_visibility = (
        [bool(value) for value in saved_kind_visibility]
        if (
            isinstance(saved_kind_visibility, list)
            and len(saved_kind_visibility) == KIND_SLOT_COUNT
        )
        else [args.profile != 'freecam'] * KIND_SLOT_COUNT
    )
    # The number row can still be physically/logically held while the
    # launcher is starting (notably after entering a numeric duration). Treat
    # that initial state as already consumed so a renderer kind is not toggled
    # before the user releases and presses the key deliberately.
    kind_key_was_down = [
        kind_key_pressed(kind) for kind in range(KIND_SLOT_COUNT)
    ]
    collision_probe = None
    collision_vertices = None
    collision_triangles = None
    collision_spatial_index = None
    collision_visible = True
    collision_orientation = args.collision_orientation
    collision_key_was_down = [False] * 10
    collision_updates = 0
    collision_selected_count = 0
    next_collision_update = 0.0
    collision_nearest_only = False
    collision_nearest_was_down = False
    last_reported_triangle: int | None = None
    collision_data_address = args.collision_data_address
    combined_renderer_mode = "triggers"
    combined_toggle_was_down = False
    trigger_probe = None
    trigger_data_template = None
    trigger_code_blob = None
    trigger_visibility_offset = 0
    simultaneous_probe = None
    simultaneous_vertex_pointer = 0
    climb_classes: dict[int, str] | None = None
    climb_mode = bool(resume_state.get("climb_mode", False))
    climb_toggle_was_down = pressed("C")
    saved_flag_mode = resume_state.get("flag_mode", 2 if args.profile == 'freecam' else 0)
    flag_mode = (
        int(saved_flag_mode)
        if isinstance(saved_flag_mode, int) and saved_flag_mode in (0, 1, 2)
        else 0
    )
    flag_toggle_was_down = False
    flag_trigger_count = 0
    saved_volume_selection = resume_state.get("volume_selection_index", 0)
    volume_selection_index = (
        int(saved_volume_selection)
        if (
            resume_same_world
            and isinstance(saved_volume_selection, int)
            and 0 <= saved_volume_selection < args.volume_count
        )
        else 0
    )
    volume_solo_enabled = bool(
        resume_same_world
        and volume_selection_requested
        and resume_state.get("volume_solo_enabled", False)
    )
    volume_selection_was_down = pressed("N")
    volume_solo_was_down = pressed("M")
    saved_terrain_visibility = resume_state.get("terrain_visibility")
    terrain_visibility = (
        [bool(value) for value in saved_terrain_visibility]
        if (
            isinstance(saved_terrain_visibility, list)
            and len(saved_terrain_visibility) == KIND_SLOT_COUNT
        )
        else [True] * KIND_SLOT_COUNT
    )
    terrain_master_visible = bool(
        resume_state.get("terrain_master_visible", args.profile != 'freecam')
    )
    terrain_toggle_was_down = False
    if (
        args.collision_data_address
        or combined_requested
        or simultaneous_requested
    ):
        import collision_render_probe as collision_probe_module

        collision_probe = collision_probe_module
        (
            collision_object,
            collision_vertices,
            collision_triangles,
        ) = collision_probe.read_collision(client)
        collision_spatial_index = collision_probe.CollisionSpatialIndex(
            collision_vertices, collision_triangles
        )
        if simultaneous_requested:
            import simultaneous_debug_renderer as simultaneous_probe_module

            simultaneous_probe = simultaneous_probe_module
            simultaneous_vertex_pointer = (
                simultaneous_probe.collision_vertex_pointer(
                    client, collision_object
                )
            )
            installed_hook = collision_probe.jal(
                args.simultaneous_code_address
            )
            current_hook = client.read(
                collision_probe.MAIN_VIEW_HOOK_ADDRESS, 32
            )
            if current_hook != installed_hook:
                raise RuntimeError(
                    "simultaneous main-view hook does not match its code block"
                )
            code_marker = client.read_block(
                args.simultaneous_code_address
                + collision_probe.MARKER_OFFSET,
                16,
                progress=False,
            )
            data_marker = client.read_block(
                args.simultaneous_data_address
                + collision_probe.MARKER_OFFSET,
                16,
                progress=False,
            )
            if code_marker != collision_probe.MARKER:
                raise RuntimeError(
                    "simultaneous renderer code marker mismatch"
                )
            if data_marker != simultaneous_probe.DATA_MARKER:
                raise RuntimeError(
                    "simultaneous renderer data marker mismatch"
                )
            if obb_layout_supported:
                obb_layout = client.read_block(
                    args.simultaneous_data_address
                    + simultaneous_probe.OBB_INDEX_OFFSET,
                    len(simultaneous_probe.OBB_LINE_INDICES) + 8,
                    progress=False,
                )
                expected_obb_layout = (
                    simultaneous_probe.OBB_LINE_INDICES
                    + bytes(simultaneous_probe.OBJECT_OBB_COLOR)
                    + struct.pack("<f", simultaneous_probe.OBB_COORD_SCALE)
                )
                if obb_layout != expected_obb_layout:
                    raise RuntimeError(
                        "experimental object OBB data layout mismatch"
                    )
            flag_header = client.read_block(
                args.flag_render_state_address,
                simultaneous_probe.FLAG_RENDER_HEADER.size,
                progress=False,
            )
            (
                flag_magic,
                flag_trigger_count,
                _flag_current_count,
                installed_volume_selection,
            ) = simultaneous_probe.FLAG_RENDER_HEADER.unpack(flag_header)
            if flag_magic != simultaneous_probe.FLAG_RENDER_MAGIC:
                raise RuntimeError("native flag-render header mismatch")
            if not 0 <= flag_trigger_count <= 255:
                raise RuntimeError("native flag marker count is invalid")
            if (
                volume_selection_requested
                and installed_volume_selection
                != simultaneous_probe.VOLUME_SELECTION_ALL
            ):
                raise RuntimeError("native volume selection did not start reset")
            climb_classes = simultaneous_probe.classify_climb_surfaces(
                collision_vertices,
                collision_triangles,
                args.collision_wall_normal_z,
            )
            climb_counts: dict[str, int] = {}
            for climb_class in climb_classes.values():
                climb_counts[climb_class] = climb_counts.get(climb_class, 0) + 1
            print(
                "climb diagnostics: "
                + ", ".join(
                    f"{name}={count}"
                    for name, count in sorted(climb_counts.items())
                ),
                flush=True,
            )
            initial_flag_count = (
                flag_trigger_count
                if flag_mode in (0, 1) and not volume_solo_enabled
                else 0
            )
            client.write(
                args.flag_render_state_address + 5,
                initial_flag_count,
                8,
            )
            if volume_selection_requested:
                client.write(
                    args.volume_selection_address,
                    (
                        volume_selection_index
                        + 1 if volume_solo_enabled
                        else VOLUME_SELECTION_ALL
                    ),
                    8,
                )
        elif combined_requested:
            import all_volumes_probe as trigger_probe_module

            trigger_probe = trigger_probe_module
            collision_data_address = args.combined_data_address
            installed_hook = collision_probe.jal(args.combined_code_address)
            current_hook = client.read(
                collision_probe.MAIN_VIEW_HOOK_ADDRESS, 32
            )
            if current_hook != installed_hook:
                raise RuntimeError(
                    f"combined main-view hook mismatch: 0x{current_hook:08X}"
                )
            code_marker = client.read_block(
                args.combined_code_address + collision_probe.MARKER_OFFSET,
                16,
                progress=False,
            )
            data_marker = client.read_block(
                args.combined_data_address + collision_probe.MARKER_OFFSET,
                16,
                progress=False,
            )
            if code_marker != collision_probe.MARKER:
                raise RuntimeError("combined renderer code marker mismatch")
            if data_marker != trigger_probe.DATA_MARKER:
                raise RuntimeError("combined trigger data marker mismatch")

            trigger_document = json.loads(
                Path(args.combined_volumes).read_text(encoding="utf-8")
            )
            trigger_rows = trigger_probe.select_volumes(
                trigger_document, tuple(position), 0.0, 0
            )
            trigger_volumes = [volume for _distance, volume in trigger_rows]
            (
                trigger_data_template,
                trigger_index_offset,
                trigger_kind_offset,
                trigger_visibility_offset,
            ) = trigger_probe.build_compact_data(trigger_volumes)
            if (
                args.combined_data_address + trigger_visibility_offset
                != args.kind_visibility_address
            ):
                raise RuntimeError(
                    "combined trigger visibility address does not match layout"
                )
            trigger_code_blob, _trigger_code_size = (
                trigger_probe.build_code_block(
                    args.combined_code_address,
                    args.combined_data_address,
                    args.combined_data_address + trigger_index_offset,
                    args.combined_data_address + trigger_kind_offset,
                    args.combined_data_address + trigger_visibility_offset,
                    len(trigger_volumes),
                )
            )
        else:
            marker = client.read_block(
                args.collision_data_address + collision_probe.MARKER_OFFSET,
                16,
                progress=False,
            )
            if marker != collision_probe.DATA_MARKER:
                raise RuntimeError("collision renderer data marker mismatch")
        print(
            f"collision_updater object=0x{collision_object:08X} "
            f"triangles={len(collision_triangles)} "
            f"vertices={len(collision_vertices)} "
            f"data=0x{collision_data_address:08X} "
            f"simultaneous={simultaneous_requested}",
            flush=True,
        )

    if args.kind_visibility_address:
        for kind, visible in enumerate(kind_visibility):
            client.write(
                args.kind_visibility_address + kind,
                int(visible),
                8,
            )

    transition_active = False
    transition_handoff = False
    transition_stop_requested = False
    transition_reason = ""
    transition_candidate = None
    transition_candidate_since = 0.0
    camera_missing_since = 0.0
    transition_previous = time.monotonic()
    transition_next_broadcast = 0.0
    transition_first_camera_seen = False
    transition_postload_boundary_seen = False
    transition_player_unavailable_seen = False
    transition_player_available = False
    transition_player_human = 0
    transition_player_position: tuple[float, float, float] | None = None
    transition_next_player_probe = 0.0
    transition_heap_probe_next = 0.0
    transition_heap_probe_failures = 0

    def disarm_renderer_for_transition(reason: str) -> None:
        """Stop EE hook execution immediately, without calling malloc/free."""
        nonlocal camera_override_begin_hook_installed
        nonlocal camera_override_enabled
        if controller_input is not None:
            controller_input.pad.close()
        if native_camera is not None:
            native_camera.close()
        code_address = args.lifecycle_renderer_code_address
        if code_address:
            installed_main_hook = mips_jal(code_address)
            current_main_hook = client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
            if current_main_hook == installed_main_hook:
                client.write(
                    MAIN_VIEW_HOOK_ADDRESS,
                    MAIN_VIEW_HOOK_ORIGINAL,
                    32,
                )
            elif current_main_hook != MAIN_VIEW_HOOK_ORIGINAL:
                raise RuntimeError(
                    "cannot safely disarm unexpected main-view hook: "
                    f"0x{current_main_hook:08X}"
                )

            installed_camera_hook = mips_jal(
                code_address + CAMERA_OVERRIDE_STUB_OFFSET
            )
            current_camera_hook = client.read(
                CAMERA_BEGIN_UPDATE_HOOK,
                32,
            )
            if current_camera_hook == installed_camera_hook:
                client.write(
                    CAMERA_BEGIN_UPDATE_HOOK,
                    CAMERA_BEGIN_UPDATE_ORIGINAL,
                    32,
                )
            elif current_camera_hook != CAMERA_BEGIN_UPDATE_ORIGINAL:
                raise RuntimeError(
                    "cannot safely disarm unexpected camera hook: "
                    f"0x{current_camera_hook:08X}"
                )

            if (
                client.read(MAIN_VIEW_HOOK_ADDRESS, 32)
                != MAIN_VIEW_HOOK_ORIGINAL
                or client.read(CAMERA_BEGIN_UPDATE_HOOK, 32)
                != CAMERA_BEGIN_UPDATE_ORIGINAL
            ):
                raise RuntimeError(
                    "transition-time renderer disarm did not verify"
                )
        camera_override_begin_hook_installed = False
        camera_override_enabled = False
        print(
            "WORLD TRANSITION: renderer disarmed without heap cleanup; "
            f"waiting for the next compatible camera ({reason})",
            flush=True,
        )

    def begin_world_transition(reason: str) -> None:
        nonlocal air_stand_controller
        nonlocal transition_active
        nonlocal transition_reason
        nonlocal camera_available
        nonlocal mouse_engaged
        nonlocal transition_player_unavailable_seen
        nonlocal transition_player_available
        nonlocal transition_player_human
        nonlocal transition_player_position
        nonlocal transition_next_player_probe
        nonlocal transition_heap_probe_next
        nonlocal transition_heap_probe_failures
        if transition_active:
            return
        if controller_input is not None:
            controller_input.pad.release()
        if visibility_hotkeys is not None:
            visibility_hotkeys.close()
        if world_time is not None and world_time.active:
            restored = world_time.disable()
            print(
                "WORLD TIME: restored before world transition "
                f"(timestep={restored:.9g})",
                flush=True,
            )
        if air_stand_controller is not None:
            air_stand_controller.close()
            air_stand_controller = None
            print(
                "WORLD TRANSITION: player air-stand guard disarmed",
                flush=True,
            )
        if destroyed_ai_controller is not None:
            # Restore any still-valid old-world brains before forgetting their
            # pointers.  The requested mode itself is carried to the relaunch.
            try:
                destroyed_ai_controller.restore(
                    client,
                    (
                        destroyed_cp4_sampler.latest_snapshot
                        if destroyed_cp4_sampler is not None
                        else None
                    ),
                )
            except Exception as error:
                print(
                    "WARNING: could not restore AI before loading: "
                    f"{error}",
                    flush=True,
                )
            destroyed_ai_controller.abandon()
        disarm_renderer_for_transition(reason)
        transition_active = True
        transition_reason = reason
        camera_available = False
        transition_player_unavailable_seen = False
        transition_player_available = False
        transition_player_human = 0
        transition_player_position = None
        transition_next_player_probe = 0.0
        transition_heap_probe_next = 0.0
        transition_heap_probe_failures = 0
        if player_controller is not None:
            # The old entity may already have been destroyed.  Do not call
            # set_attached(), close(), or any method that writes through it.
            player_controller.attached = False
        if mouse_engaged:
            ctypes.windll.user32.SetCursorPos(
                original_cursor.x,
                original_cursor.y,
            )
            mouse_engaged = False

    def update_transition_player(now: float) -> None:
        """Track the loader's player-absent/player-returned lifecycle signal."""
        nonlocal transition_player_unavailable_seen
        nonlocal transition_player_available
        nonlocal transition_player_human
        nonlocal transition_player_position
        nonlocal transition_next_player_probe

        if now < transition_next_player_probe:
            return
        was_available = transition_player_available
        try:
            (
                transition_player_human,
                transition_player_position,
            ) = read_player_position(client)
            transition_player_available = True
            transition_next_player_probe = now + 0.05
            if transition_player_unavailable_seen and not was_available:
                print(
                    "WORLD TRANSITION: live player returned; waiting for "
                    "a coherent follow camera and collision world",
                    flush=True,
                )
        except RuntimeError:
            transition_player_human = 0
            transition_player_position = None
            transition_player_available = False
            transition_next_player_probe = now + 0.1
            if not transition_player_unavailable_seen:
                transition_player_unavailable_seen = True
                print(
                    "WORLD TRANSITION: live player is unavailable; "
                    "loading phase confirmed",
                    flush=True,
                )

    def update_transition_camera(now: float) -> int | None:
        """Keep freecam control on cinematic cameras while the world rebuilds."""
        nonlocal camera_object
        nonlocal camera_transform
        nonlocal camera_available
        nonlocal position
        nonlocal position_w
        nonlocal quaternion
        nonlocal frustum_slopes
        nonlocal freecam_enabled
        nonlocal camera_toggle_was_down
        nonlocal mouse_engaged
        nonlocal transition_previous
        nonlocal transition_next_broadcast
        nonlocal last_written_block

        try:
            live_object = client.read(CAMERA_OBJECT_POINTER, 32)
            live_transform = resolve_camera_transform(
                client,
                camera_object=live_object,
            )
            live_raw = client.read_block(
                live_transform,
                0x60,
                progress=False,
            )
            live_values = list(struct.unpack_from("<8f", live_raw))
            live_frustum = struct.unpack_from("<2f", live_raw, 0x4C)
            if not all(
                math.isfinite(value)
                for value in (*live_values, *live_frustum)
            ):
                raise RuntimeError(
                    "transition camera contains non-finite values"
                )
            live_vtable = client.read(live_object, 32)
        except RuntimeError:
            camera_available = False
            transition_previous = now
            if mouse_engaged:
                ctypes.windll.user32.SetCursorPos(
                    original_cursor.x,
                    original_cursor.y,
                )
                mouse_engaged = False
            return None

        rebound = not camera_available or live_object != camera_object
        camera_object = live_object
        camera_transform = live_transform
        camera_available = True
        position_w = live_values[3]
        frustum_slopes = live_frustum
        if rebound:
            print(
                "WORLD TRANSITION: freecam rebound to compatible "
                f"camera 0x{camera_object:08X} "
                f"vtable=0x{live_vtable:08X}",
                flush=True,
            )

        toggle_down = pressed("V") if args.camera_mode_toggle else False
        if toggle_down and not camera_toggle_was_down:
            freecam_enabled = not freecam_enabled
            key_polls["V"] += 1
            if freecam_enabled or mouse_engaged:
                position = live_values[:3]
                quaternion = normalize_quaternion(
                    tuple(live_values[4:8])
                )
            if not freecam_enabled and mouse_engaged:
                ctypes.windll.user32.SetCursorPos(
                    original_cursor.x,
                    original_cursor.y,
                )
                mouse_engaged = False
            print(
                "camera mode during transition: "
                + ("FREECAM" if freecam_enabled else "NORMAL"),
                flush=True,
            )
        camera_toggle_was_down = toggle_down

        dt = min(max(0.0, now - transition_previous), 0.05)
        transition_previous = now
        if not freecam_enabled:
            position = live_values[:3]
            quaternion = normalize_quaternion(
                tuple(live_values[4:8])
            )

        yaw = 0.0
        pitch = 0.0
        look_step = math.radians(args.look_speed) * dt
        for key, yaw_sign, pitch_sign in (
            ("H", 1.0, 0.0),
            ("K", -1.0, 0.0),
            ("U", 0.0, 1.0),
            ("J", 0.0, -1.0),
        ):
            if freecam_enabled and pressed(key):
                key_polls[key] += 1
                yaw += look_step * yaw_sign
                pitch += look_step * pitch_sign

        foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        center = window_center(game_window)
        if (
            freecam_enabled
            and not (controller_input is not None and controller_input.controller_mode)
            and game_window
            and foreground == game_window
            and center is not None
        ):
            cursor = Point()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
            if mouse_engaged:
                delta_x = cursor.x - center[0]
                delta_y = cursor.y - center[1]
                if abs(delta_x) < 1000 and abs(delta_y) < 1000:
                    yaw -= math.radians(
                        delta_x * args.mouse_sensitivity
                    )
                    pitch -= math.radians(
                        delta_y * args.mouse_sensitivity
                    )
            ctypes.windll.user32.SetCursorPos(*center)
            mouse_engaged = True
        elif mouse_engaged:
            ctypes.windll.user32.SetCursorPos(
                original_cursor.x,
                original_cursor.y,
            )
            mouse_engaged = False

        if yaw:
            yaw_rotation = quaternion_axis_angle(
                (0.0, 0.0, 1.0),
                yaw,
            )
            quaternion = normalize_quaternion(
                quaternion_multiply(yaw_rotation, quaternion)
            )
        if pitch:
            pitch_rotation = quaternion_axis_angle(
                (1.0, 0.0, 0.0),
                pitch,
            )
            candidate = normalize_quaternion(
                quaternion_multiply(quaternion, pitch_rotation)
            )
            candidate_forward = rotate(
                candidate,
                (0.0, 1.0, 0.0),
            )
            if abs(candidate_forward[2]) < 0.995:
                quaternion = candidate

        right = normalize(rotate(quaternion, (1.0, 0.0, 0.0)))
        forward = normalize(rotate(quaternion, (0.0, 1.0, 0.0)))
        movement = [0.0, 0.0, 0.0]
        for key, axis, sign in (
            ("W", forward, 1.0),
            ("S", forward, -1.0),
            ("D", right, 1.0),
            ("A", right, -1.0),
            ("E", world_up, 1.0),
            ("Q", world_up, -1.0),
        ):
            if freecam_enabled and pressed(key):
                key_polls[key] += 1
                for index in range(3):
                    movement[index] += axis[index] * sign
        movement_length = math.sqrt(
            sum(component * component for component in movement)
        )
        transition_speed = args.speed
        if pressed("SHIFT"):
            transition_speed *= 4.0
        if pressed("CTRL"):
            transition_speed *= 0.25
        if movement_length:
            for index in range(3):
                position[index] += (
                    movement[index]
                    / movement_length
                    * transition_speed
                    * dt
                )

        block = struct.pack(
            "<8f",
            *position,
            position_w,
            *quaternion,
        )
        if freecam_enabled and not args.native_camera_code_address:
            client.write_block(camera_transform, block)
            last_written_block = block

        if broadcast is not None and now >= transition_next_broadcast:
            debug_suffix = b""
            if simultaneous_requested:
                trigger_mask = sum(
                    1 << kind
                    for kind in range(4)
                    if kind_visibility[kind]
                )
                terrain_mask = sum(
                    1 << (kind - 4)
                    for kind in range(4, 10)
                    if terrain_visibility[kind]
                )
                god_enabled = bool(
                    player_controller is not None
                    and player_controller.god_mode
                )
                debug_flags = (
                    int(terrain_master_visible)
                    | (int(freecam_enabled) << 1)
                    | (int(god_enabled) << 3)
                    | (int(player_debug_visible) << 4)
                    | ((flag_mode & 0x03) << 5)
                    | (int(climb_mode) << 7)
                )
                debug_suffix = DEBUG_STATE_HEADER.pack(
                    DEBUG_STATE_MAGIC,
                    DEBUG_STATE_VERSION,
                    trigger_mask,
                    terrain_mask,
                    debug_flags,
                    (
                        collision_selected_count
                        if terrain_master_visible
                        else 0
                    ),
                    simultaneous_probe.TERRAIN_CAPACITY,
                    0.0,
                    0.0,
                    0.0,
                    player_debug_radius,
                    0,
                )
            transition_packet = (
                block
                + struct.pack("<2f", *frustum_slopes)
                + debug_suffix
                + POSITION_STATE_HEADER.pack(
                    POSITION_STATE_MAGIC,
                    POSITION_STATE_VERSION,
                    int(transition_player_position is not None),
                    *(transition_player_position or (0.0, 0.0, 0.0)),
                )
                + VOLUME_SELECTION_HEADER.pack(
                    VOLUME_SELECTION_MAGIC,
                    VOLUME_SELECTION_VERSION,
                    int(volume_solo_enabled),
                    volume_selection_index,
                    args.volume_count,
                )
                + destroyed_cp4_packet
                + KIND_VISIBILITY_MAGIC
                + bytes(kind_visibility)
            )
            broadcast.sendto(
                transition_packet,
                ("127.0.0.1", args.broadcast_port),
            )
            transition_next_broadcast = now + 1.0 / 60.0
        return live_vtable

    def write_transition_state() -> None:
        if args.transition_state is None:
            return
        payload = {
            "version": 1,
            "world_id": args.world_id,
            "position": list(position),
            "quaternion": list(quaternion),
            "freecam_enabled": freecam_enabled,
            "controller_mode": bool(controller_input and controller_input.controller_mode),
            "hud_toggle": controller_input.hud_toggle if controller_input is not None else 0,
            "carry_player": carry_preference,
            "air_stand": air_stand_preference,
            "god_mode": (
                player_controller.god_mode
                if player_controller is not None
                else bool(resume_state.get("god_mode", True))
            ),
            "player_debug_visible": player_debug_visible,
            "object_debug_visible": object_debug_visible,
            "kind_visibility": list(kind_visibility),
            "terrain_visibility": list(terrain_visibility),
            "terrain_master_visible": terrain_master_visible,
            "climb_mode": climb_mode,
            "flag_mode": flag_mode,
            "volume_selection_index": volume_selection_index,
            "volume_solo_enabled": volume_solo_enabled,
            "destroyed_ai_freeze_mode": destroyed_ai_mode,
            "transition_reason": transition_reason,
        }
        args.transition_state.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.transition_state.with_suffix(
            args.transition_state.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        temporary.replace(args.transition_state)

    def install_combined_renderer(
        target: str, camera_position: tuple[float, float, float]
    ) -> int:
        if not combined_requested:
            raise RuntimeError("combined renderer switching was not configured")
        assert collision_probe is not None
        assert trigger_probe is not None
        assert trigger_data_template is not None
        assert trigger_code_blob is not None
        assert collision_vertices is not None
        assert collision_triangles is not None

        installed_hook = collision_probe.jal(args.combined_code_address)
        current_hook = client.read(collision_probe.MAIN_VIEW_HOOK_ADDRESS, 32)
        if current_hook not in (
            collision_probe.MAIN_VIEW_HOOK_ORIGINAL,
            installed_hook,
        ):
            raise RuntimeError(
                f"unexpected combined main-view hook: 0x{current_hook:08X}"
            )
        client.write(
            collision_probe.MAIN_VIEW_HOOK_ADDRESS,
            collision_probe.MAIN_VIEW_HOOK_ORIGINAL,
            32,
        )
        if (
            client.read(collision_probe.MAIN_VIEW_HOOK_ADDRESS, 32)
            != collision_probe.MAIN_VIEW_HOOK_ORIGINAL
        ):
            raise RuntimeError("combined hook restoration did not verify")

        if target == "triggers":
            data_blob = bytearray(trigger_data_template)
            data_blob[
                trigger_visibility_offset:
                trigger_visibility_offset + KIND_SLOT_COUNT
            ] = bytes(kind_visibility)
            code_blob = trigger_code_blob
            selected_count = len(trigger_volumes)
        elif target == "collision":
            if collision_nearest_only:
                collision_rows = collision_probe.select_nearest_triangle(
                    collision_vertices,
                    collision_triangles,
                    camera_position,
                    collision_orientation,
                    args.collision_wall_normal_z,
                )
            else:
                collision_rows = collision_probe.select_triangles(
                    collision_vertices,
                    collision_triangles,
                    camera_position,
                    args.collision_radius,
                    args.collision_limit,
                    collision_orientation,
                    args.collision_wall_normal_z,
                    spatial_index=collision_spatial_index,
                )
            data_blob = bytearray(
                collision_probe.build_data_block(
                    collision_rows, collision_orientation
                )
            )
            if not collision_visible:
                struct.pack_into("<I", data_blob, 0, 0)
            code_blob, _collision_code_size = (
                collision_probe.build_code_block(
                    args.combined_code_address,
                    args.combined_data_address,
                    len(collision_rows),
                )
            )
            selected_count = len(collision_rows)
        else:
            raise ValueError(f"unknown combined renderer mode: {target}")

        client.write_block(args.combined_data_address, bytes(data_blob))
        client.write_block(args.combined_code_address, code_blob)
        client.write(
            collision_probe.MAIN_VIEW_HOOK_ADDRESS, installed_hook, 32
        )
        if client.read(collision_probe.MAIN_VIEW_HOOK_ADDRESS, 32) != installed_hook:
            raise RuntimeError("combined hook installation did not verify")
        print(f"debug renderer mode: {target}", flush=True)
        return selected_count
    from ingame_volume_probe import probe_heap_callsite
    from world_lifecycle import read_world_identity

    f10_requested = threading.Event()
    f10_watchdog_shutdown = threading.Event()
    f10_watchdog = threading.Thread(
        target=watch_f10,
        args=(f10_requested, f10_watchdog_shutdown),
        name="the-warriors-f10-watchdog",
        daemon=True,
    )
    f10_watchdog.start()
    try:
        if controller_input is not None:
            controller_input.start()
        if args.native_camera_code_address and not transition_active:
            if args.camera_override_code_address:
                raise ValueError("Native camera and old post-BeginUpdate diagnostic are mutually exclusive")
            from native_freecam import NativeFreecam
            native_camera = NativeFreecam(client, args.native_camera_code_address)
            native_camera.install()
            native_camera.update(position, quaternion, freecam_enabled)
            print("NATIVE FREECAM: render-synchronous transform; publish only on change", flush=True)
        if args.camera_override_code_address and not transition_active:
            current_camera_hook = client.read(
                CAMERA_BEGIN_UPDATE_HOOK,
                32,
            )
            if current_camera_hook != CAMERA_BEGIN_UPDATE_ORIGINAL:
                raise RuntimeError(
                    "camera BeginUpdate hook is not original: "
                    f"0x{current_camera_hook:08X}"
                )
            client.write(
                CAMERA_BEGIN_UPDATE_HOOK,
                mips_jal(
                    args.camera_override_code_address
                    + CAMERA_OVERRIDE_STUB_OFFSET
                ),
                32,
            )
            camera_override_begin_hook_installed = True
        if (
            args.player_debug_toggle
            or args.object_debug_toggle
            or args.climb_object_support
        ):
            from player_debug_probe import PhysicsShapeSampler

            player_debug_sampler = PhysicsShapeSampler(client)
            object_inventory = (
                "; "
                f"{sum(map(len, player_debug_sampler.obb_objects_by_page.values()))} "
                "type-1 OBBs across "
                f"{len(player_debug_sampler.obb_objects_by_page)} pages"
                "; "
                f"{len(player_debug_sampler.climb_objects)} "
                "confirmed dynamic climb objects"
                if obb_layout_supported
                else ""
            )
            print(
                "player debug inventory: "
                f"{sum(map(len, player_debug_sampler.objects_by_page.values()))} "
                f"native bounds across "
                f"{len(player_debug_sampler.objects_by_page)} pages"
                + object_inventory,
                flush=True,
            )
        if args.carry_player:
            from player_noclip import (
                PlayerNoclipController,
                read_player_position,
            )

            player_controller = PlayerNoclipController(
                client,
                offset_forward=args.player_offset_forward,
                offset_up=args.player_offset_up,
                safety=args.player_safety,
                god_mode=(
                    bool(resume_state.get("god_mode", True))
                    if args.player_god_mode
                    else None
                ),
                no_target=(
                    carry_preference if args.player_no_target else None
                ),
                attached=carry_preference,
                neutralize_gravity=not args.keep_player_gravity,
                restore_position_on_close=args.restore_player_on_exit,
            )
            movement_label = (
                f"0x{player_controller.movement_address:08X}"
                if player_controller.movement_address is not None
                else "none"
            )
            print(
                f"player_noclip human=0x{player_controller.human:08X} "
                f"movement={movement_label} "
                f"offset=({args.player_offset_forward:g} forward, "
                f"{args.player_offset_up:g} up) safety={args.player_safety} "
                f"god={player_controller.god_mode} "
                f"no_target={player_controller.no_target} "
                f"attached={player_controller.attached} "
                f"gravity_neutralized={not args.keep_player_gravity}",
                flush=True,
            )
        if args.player_air_stand_toggle:
            from player_air_stand import PlayerAirStandController

            air_stand_controller = PlayerAirStandController(
                client,
                enabled=air_stand_preference,
                embedded_code_address=args.simultaneous_code_address,
            )
            print(
                "player air stand: "
                + ("ON" if air_stand_controller.enabled else "OFF")
                + " (Y)",
                flush=True,
            )
        # Renderer/collision/player initialization happens before the main
        # loop and must not count as a runtime freecam stall.
        if args.ui_panels_state is not None:
            from game_ui_panels import PanelHotkeys
            from map_visibility_control import VisibilityHotkeys, status_path

            panel_hotkeys = PanelHotkeys(client, args.ui_panels_state)
            visibility_hotkeys = VisibilityHotkeys(client, status_path(args.ui_panels_state),
                                                  args.native_camera_code_address or None)
        if args.ready_file is not None:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.ready_file.write_text("ready\n", encoding="ascii")
        previous = time.monotonic()
        while transition_active or time.monotonic() < deadline:
            if args.stop_file is not None and args.stop_file.exists():
                f10_requested.set()
            if controller_input is not None:
                keyboard_v = keyboard_pressed('V')
                controller_input.sample(
                    time.monotonic(), freecam_enabled,
                    game_window_has_focus(game_window), transition_active,
                    keyboard_v and not keyboard_v_was_down,
                )
                keyboard_v_was_down = keyboard_v
                if controller_input.exit_requested:
                    f10_requested.set()
                controller_input.publish(freecam_enabled, bool(world_time and world_time.active))
            if panel_hotkeys is not None and panel_hotkeys.poll():
                f10_requested.set()
            if visibility_hotkeys is not None:
                visibility_hotkeys.poll(active=not transition_active)
            if transition_active:
                if f10_requested.is_set() or pressed("F10"):
                    transition_stop_requested = True
                    key_polls["F10"] += 1
                transition_now = time.monotonic()
                update_transition_player(transition_now)
                transition_vtable = update_transition_camera(
                    transition_now
                )
                if transition_vtable is None:
                    if (
                        transition_first_camera_seen
                        and not transition_postload_boundary_seen
                    ):
                        transition_postload_boundary_seen = True
                        print(
                            "WORLD TRANSITION: observed the post-load "
                            "camera gap; provisional follow camera rejected",
                            flush=True,
                        )
                elif not transition_first_camera_seen:
                    transition_first_camera_seen = True
                    transition_postload_boundary_seen = (
                        transition_vtable != CAMERA_VTABLE
                    )
                    print(
                        "WORLD TRANSITION: first returned camera is "
                        + (
                            "cinematic"
                            if transition_postload_boundary_seen
                            else "provisional follow; keeping freecam alive"
                        ),
                        flush=True,
                    )
                elif (
                    transition_vtable != CAMERA_VTABLE
                    and not transition_postload_boundary_seen
                ):
                    transition_postload_boundary_seen = True
                    print(
                        "WORLD TRANSITION: cinematic camera phase observed; "
                        "the next sustained follow can complete the load",
                        flush=True,
                    )

                current_world = None
                if transition_vtable == CAMERA_VTABLE:
                    try:
                        current_world = read_world_identity(
                            client,
                            require_follow_camera=True,
                        )
                    except RuntimeError:
                        current_world = None

                if (
                    not transition_postload_boundary_seen
                    and transition_player_unavailable_seen
                    and transition_player_available
                    and current_world is not None
                ):
                    transition_postload_boundary_seen = True
                    print(
                        "WORLD TRANSITION: player returned with a coherent "
                        "follow camera; treating this as a transition without "
                        "a cinematic camera phase",
                        flush=True,
                    )

                if (
                    transition_postload_boundary_seen
                    and current_world is not None
                ):
                    if current_world != transition_candidate:
                        transition_candidate = current_world
                        transition_candidate_since = transition_now
                        transition_heap_probe_next = 0.0
                        transition_heap_probe_failures = 0
                        print(
                            "WORLD TRANSITION: normal gameplay camera "
                            "detected; waiting for stability...",
                            flush=True,
                        )
                    if (
                        transition_now - transition_candidate_since
                        >= args.transition_stable_seconds
                        and transition_now >= transition_heap_probe_next
                    ):
                        try:
                            heap_callsite_ready = probe_heap_callsite(
                                client,
                                timeout=0.1,
                            )
                        except RuntimeError as error:
                            heap_callsite_ready = False
                            if transition_heap_probe_failures == 0:
                                print(
                                    "WORLD TRANSITION: heap readiness probe "
                                    f"could not run yet ({error})",
                                    flush=True,
                                )
                        if heap_callsite_ready:
                            write_transition_state()
                            transition_handoff = (
                                not transition_stop_requested
                            )
                            print(
                                "WORLD TRANSITION: gameplay is stable; "
                                + (
                                    "requesting automatic rebuild"
                                    if transition_handoff
                                    else "finishing after requested F10"
                                ),
                                flush=True,
                            )
                            break
                        transition_heap_probe_failures += 1
                        transition_heap_probe_next = (
                            time.monotonic() + 1.0
                        )
                        if (
                            transition_heap_probe_failures == 1
                            or transition_heap_probe_failures % 10 == 0
                        ):
                            print(
                                "WORLD TRANSITION: world looks coherent but "
                                "the safe heap callsite is inactive; keeping "
                                "freecam alive",
                                flush=True,
                            )
                else:
                    transition_candidate = None
                    transition_candidate_since = 0.0
                    transition_heap_probe_next = 0.0
                    transition_heap_probe_failures = 0
                time.sleep(max(0.0, 1.0 / min(args.hz, 120.0)))
                continue
            if f10_requested.is_set() or pressed("F10"):
                key_polls["F10"] += 1
                break
            control_now = time.monotonic()
            if (
                args.destroyed_cp4_ai_freeze_hotkeys
                and destroyed_cp4_sampler is not None
                and destroyed_ai_controller is not None
            ):
                from destroyed_cp4_ai_control import (
                    MODE_ALL,
                    MODE_EXCEPT_OOB,
                    MODE_OFF,
                )

                ai_all_down = pressed("F6")
                ai_except_down = pressed("F7")
                requested_mode = None
                if ai_all_down and not destroyed_ai_all_was_down:
                    requested_mode = (
                        MODE_OFF
                        if destroyed_ai_mode == MODE_ALL
                        else MODE_ALL
                    )
                elif (
                    ai_except_down
                    and not destroyed_ai_except_was_down
                ):
                    requested_mode = (
                        MODE_OFF
                        if destroyed_ai_mode == MODE_EXCEPT_OOB
                        else MODE_EXCEPT_OOB
                    )
                destroyed_ai_all_was_down = ai_all_down
                destroyed_ai_except_was_down = ai_except_down
                if requested_mode is not None:
                    if destroyed_cp4_sampler.latest_snapshot is None:
                        destroyed_cp4_packet = (
                            destroyed_cp4_sampler.sample(client)
                        )
                    latest = destroyed_cp4_sampler.latest_snapshot
                    frozen, restored = destroyed_ai_controller.set_mode(
                        client,
                        latest,
                        requested_mode,
                    )
                    destroyed_ai_mode = requested_mode
                    destroyed_cp4_sampler.set_ai_freeze_mode(
                        destroyed_ai_mode
                    )
                    key_polls[
                        "F6" if ai_all_down else "F7"
                    ] += 1
                    print(
                        "DESTROYED CP4 AI "
                        f"mode={destroyed_ai_mode} frozen={frozen} "
                        f"restored={restored}",
                        flush=True,
                    )
                    next_destroyed_ai_enforce = 0.0
                    next_destroyed_cp4_update = 0.0
                if (
                    destroyed_ai_mode != MODE_OFF
                    and control_now >= next_destroyed_ai_enforce
                ):
                    try:
                        destroyed_ai_controller.enforce(
                            client,
                            destroyed_cp4_sampler.latest_snapshot,
                        )
                    except RuntimeError as error:
                        print(
                            f"WARNING: could not keep AI disabled: {error}",
                            flush=True,
                        )
                    next_destroyed_ai_enforce = control_now + 1.0 / 30.0
            if human_watcher is not None:
                human_watch_count = human_watcher.poll(
                    client,
                    control_now,
                    position,
                    quaternion,
                    frustum_slopes,
                )
                if (
                    args.human_watch_slow_after
                    and human_watch_count >= args.human_watch_slow_after
                    and not human_watch_slowed
                ):
                    assert world_time is not None
                    if not world_time.active:
                        original, target = world_time.enable()
                        print(
                            "HUMAN_WATCH_SLOWDOWN "
                            f"matches={human_watch_count} "
                            f"timestep={original:.9g}->{target:.9g}",
                            flush=True,
                        )
                    human_watch_slowed = True
                    next_world_time_enforce = 0.0
            if world_time is not None:
                if not args.disable_world_time_key:
                    toggle_down = pressed("P")
                    if toggle_down and not world_time_toggle_was_down:
                        key_polls["P"] += 1
                        if world_time.active:
                            restored = world_time.disable()
                            print(
                                "world time: NORMAL "
                                f"(timestep={restored:.9g})",
                                flush=True,
                            )
                        else:
                            original, target = world_time.enable()
                            print(
                                "world time: "
                                + (
                                    "PAUSED"
                                    if args.world_time_scale == 0.0
                                    else (
                                        "PRACTICALLY PAUSED "
                                        f"({args.world_time_scale * 100:.4g}%)"
                                    )
                                )
                                + f" (timestep {original:.9g} -> {target:.9g})",
                                flush=True,
                            )
                        next_world_time_enforce = 0.0
                    world_time_toggle_was_down = toggle_down
                if (
                    world_time.active
                    and control_now >= next_world_time_enforce
                ):
                    world_time.enforce()
                    next_world_time_enforce = control_now + 0.05
            live_camera_object = client.read(CAMERA_OBJECT_POINTER, 32)
            if (
                live_camera_object != camera_object
                or (
                    not camera_available
                    and control_now >= next_camera_recovery
                )
            ):
                previous_camera_object = camera_object
                camera_object = live_camera_object
                camera_available = False
                try:
                    candidate_transform = resolve_camera_transform(
                        client,
                        camera_object=camera_object,
                    )
                    candidate_raw = client.read_block(
                        candidate_transform,
                        0x60,
                        progress=False,
                    )
                    candidate_values = struct.unpack_from(
                        "<8f",
                        candidate_raw,
                    )
                    candidate_frustum = struct.unpack_from(
                        "<2f",
                        candidate_raw,
                        0x4C,
                    )
                    if not all(
                        math.isfinite(value)
                        for value in (*candidate_values, *candidate_frustum)
                    ):
                        raise RuntimeError(
                            "active camera contains a non-finite transform"
                        )
                    camera_transform = candidate_transform
                    position_w = candidate_values[3]
                    frustum_slopes = candidate_frustum
                    camera_available = True
                    last_camera_resolution_error = ""
                    if camera_object != previous_camera_object:
                        camera_switches += 1
                        print(
                            "camera rebound "
                            f"0x{previous_camera_object:08X} -> "
                            f"0x{camera_object:08X} "
                            f"transform=0x{camera_transform:08X}",
                            flush=True,
                        )
                    else:
                        camera_recoveries += 1
                except RuntimeError as error:
                    camera_resolution_failures += 1
                    next_camera_recovery = control_now + 0.05
                    message = str(error)
                    if message != last_camera_resolution_error:
                        print(
                            "camera temporarily unavailable; retaining "
                            f"freecam state: {message}",
                            flush=True,
                        )
                        last_camera_resolution_error = message
            if camera_available:
                camera_missing_since = 0.0
            else:
                begin_world_transition(
                    last_camera_resolution_error
                    or "logical camera unavailable"
                )
                continue
            if args.camera_mode_toggle:
                toggle_down = pressed("V")
                requested_enabled = (
                    controller_input.requested_enabled if controller_input is not None else None
                )
                if (requested_enabled is not None and requested_enabled != freecam_enabled) or (
                    controller_input is None and toggle_down and not camera_toggle_was_down
                ):
                    freecam_enabled = requested_enabled if requested_enabled is not None else not freecam_enabled
                    key_polls["V"] += 1
                    if not freecam_enabled:
                        if controller_input is not None:
                            controller_input.pad.release()
                        if args.return_to_follow_camera:
                            from follow_camera import handoff_to_follow
                            handoff_to_follow(client)
                        if player_controller is not None:
                            player_controller.set_attached(False)
                            if player_controller.manage_no_target:
                                player_controller.set_no_target(False)
                        if mouse_engaged:
                            ctypes.windll.user32.SetCursorPos(
                                original_cursor.x, original_cursor.y
                            )
                            mouse_engaged = False
                    else:
                        camera_transform = resolve_camera_transform(
                            client,
                            camera_object=camera_object,
                        )
                        camera_available = True
                        raw = client.read_block(
                            camera_transform, 0x60, progress=False
                        )
                        values = list(struct.unpack_from("<8f", raw))
                        position = values[:3]
                        position_w = values[3]
                        quaternion = normalize_quaternion(
                            tuple(values[4:8])
                        )
                        frustum_slopes = struct.unpack_from(
                            "<2f", raw, 0x4C
                        )
                        if player_controller is not None:
                            player_controller.set_attached(
                                carry_preference
                            )
                            if player_controller.manage_no_target:
                                player_controller.set_no_target(
                                    carry_preference
                                )
                        previous = control_now
                    state = "FREECAM" if freecam_enabled else "NORMAL"
                    print(f"camera mode: {state}", flush=True)
                camera_toggle_was_down = toggle_down
            if player_controller is not None:
                toggle_down = pressed("F8")
                if toggle_down and not player_toggle_was_down:
                    if freecam_enabled:
                        carry_preference = not carry_preference
                        attached = player_controller.set_attached(
                            carry_preference
                        )
                        if player_controller.manage_no_target:
                            player_controller.set_no_target(attached)
                        key_polls["F8"] += 1
                        state = "ATTACHED" if attached else "RELEASED"
                        print(f"player noclip: {state}", flush=True)
                        next_player_update = 0.0
                    else:
                        print(
                            "player noclip: F8 ignored in normal camera mode",
                            flush=True,
                        )
                player_toggle_was_down = toggle_down
                if air_stand_controller is not None:
                    toggle_down = pressed("Y")
                    if toggle_down and not air_stand_toggle_was_down:
                        air_stand_preference = (
                            air_stand_controller.toggle()
                        )
                        key_polls["Y"] += 1
                        state = "ON" if air_stand_preference else "OFF"
                        print(f"player air stand: {state}", flush=True)
                    air_stand_toggle_was_down = toggle_down
                if args.player_god_mode:
                    toggle_down = pressed("G")
                    if toggle_down and not god_toggle_was_down:
                        enabled = player_controller.toggle_god_mode()
                        key_polls["G"] += 1
                        state = "ON" if enabled else "OFF"
                        print(f"god mode: {state}", flush=True)
                    god_toggle_was_down = toggle_down
                if args.player_debug_toggle:
                    toggle_down = pressed("T")
                    if toggle_down and not player_debug_toggle_was_down:
                        player_debug_visible = not player_debug_visible
                        key_polls["T"] += 1
                        if (
                            player_debug_visible
                            and player_debug_sampler is None
                        ):
                            from player_debug_probe import (
                                PhysicsShapeSampler,
                            )

                            player_debug_sampler = PhysicsShapeSampler(
                                client
                            )
                            next_player_debug_update = 0.0
                        if (
                            not player_debug_visible
                            and simultaneous_probe is not None
                        ):
                            simultaneous_probe.update_player_debug_data(
                                client,
                                args.simultaneous_code_address,
                                b"",
                                b"",
                                0,
                                enabled=False,
                            )
                        state = "ON" if player_debug_visible else "OFF"
                        print(f"player debug: {state}", flush=True)
                    player_debug_toggle_was_down = toggle_down
                if args.object_debug_toggle:
                    toggle_down = pressed("B")
                    if toggle_down and not object_debug_toggle_was_down:
                        object_debug_visible = not object_debug_visible
                        next_object_debug_update = 0.0
                        key_polls["B"] += 1
                        state = "ON" if object_debug_visible else "OFF"
                        print(f"object OBB debug: {state}", flush=True)
                    object_debug_toggle_was_down = toggle_down
                if control_now >= next_player_refresh:
                    try:
                        player_controller.refresh()
                        if air_stand_controller is not None:
                            air_stand_controller.refresh_human()
                    except RuntimeError as error:
                        begin_world_transition(str(error))
                        continue
                    next_player_refresh = control_now + 0.25
            if combined_requested:
                toggle_down = combined_mode_key_pressed()
                if toggle_down and not combined_toggle_was_down:
                    target_mode = (
                        "collision"
                        if combined_renderer_mode == "triggers"
                        else "triggers"
                    )
                    combined_selected = install_combined_renderer(
                        target_mode, tuple(position)
                    )
                    combined_renderer_mode = target_mode
                    key_polls["MODE_TOGGLE"] += 1
                    if target_mode == "collision":
                        collision_selected_count = combined_selected
                        next_collision_update = 0.0
                combined_toggle_was_down = toggle_down
            collision_filter_changed = False
            collision_controls_active = (
                collision_probe is not None
                and not simultaneous_requested
                and (
                    not combined_requested
                    or combined_renderer_mode == "collision"
                )
            )
            if simultaneous_requested:
                if volume_selection_requested:
                    selection_down = pressed("N")
                    if selection_down and not volume_selection_was_down:
                        direction = -1 if pressed("SHIFT") else 1
                        volume_selection_index = (
                            volume_selection_index + direction
                        ) % args.volume_count
                        if volume_solo_enabled:
                            client.write(
                                args.volume_selection_address,
                                volume_selection_index + 1,
                                8,
                            )
                        key_polls["N"] += 1
                        print(
                            "trigger selection: "
                            f"{volume_selection_index + 1}/{args.volume_count}",
                            flush=True,
                        )
                    volume_selection_was_down = selection_down

                    solo_down = pressed("M")
                    if solo_down and not volume_solo_was_down:
                        volume_solo_enabled = not volume_solo_enabled
                        client.write(
                            args.volume_selection_address,
                            (
                                volume_selection_index + 1
                                if volume_solo_enabled
                                else VOLUME_SELECTION_ALL
                            ),
                            8,
                        )
                        client.write(
                            args.flag_render_state_address + 5,
                            (
                                0
                                if volume_solo_enabled
                                else (
                                    flag_trigger_count
                                    if flag_mode in (0, 1)
                                    else 0
                                )
                            ),
                            8,
                        )
                        key_polls["M"] += 1
                        state = "ON" if volume_solo_enabled else "OFF"
                        print(
                            "single trigger: "
                            f"{state} "
                            f"({volume_selection_index + 1}/"
                            f"{args.volume_count})",
                            flush=True,
                        )
                    volume_solo_was_down = solo_down

                flag_down = pressed("F")
                if flag_down and not flag_toggle_was_down:
                    flag_mode = (flag_mode + 1) % 3
                    flag_marker_count = (
                        flag_trigger_count
                        if flag_mode in (0, 1) and not volume_solo_enabled
                        else 0
                    )
                    client.write(
                        args.flag_render_state_address + 5,
                        flag_marker_count,
                        8,
                    )
                    key_polls["F"] += 1
                    flag_label = ("TRIGGERS", "ALL", "OFF")[flag_mode]
                    print(
                        f"native flags: {flag_label} "
                        f"({flag_marker_count} trigger markers)",
                        flush=True,
                    )
                flag_toggle_was_down = flag_down
                climb_down = pressed("C")
                if climb_down and not climb_toggle_was_down:
                    climb_mode = not climb_mode
                    if obb_layout_supported:
                        next_object_debug_update = 0.0
                    key_polls["C"] += 1
                    collision_filter_changed = True
                    state = "ON" if climb_mode else "OFF"
                    print(f"climb diagnostics: {state}", flush=True)
                climb_toggle_was_down = climb_down
                master_down = combined_mode_key_pressed()
                if master_down and not terrain_toggle_was_down:
                    terrain_master_visible = not terrain_master_visible
                    if obb_layout_supported:
                        next_object_debug_update = 0.0
                    key_polls["MODE_TOGGLE"] += 1
                    collision_filter_changed = True
                    state = "ON" if terrain_master_visible else "OFF"
                    print(f"terrain master: {state}", flush=True)
                terrain_toggle_was_down = master_down
                for kind in range(KIND_SLOT_COUNT):
                    down = kind_key_pressed(kind)
                    if down and not kind_key_was_down[kind]:
                        if kind <= 3:
                            kind_visibility[kind] = not kind_visibility[kind]
                            client.write(
                                args.kind_visibility_address + kind,
                                int(kind_visibility[kind]),
                                8,
                            )
                            state = "ON" if kind_visibility[kind] else "OFF"
                            print(f"trigger kind {kind}: {state}", flush=True)
                        else:
                            terrain_visibility[kind] = (
                                not terrain_visibility[kind]
                            )
                            collision_filter_changed = True
                            state = (
                                "ON" if terrain_visibility[kind] else "OFF"
                            )
                            print(
                                f"terrain {kind} "
                                f"({COLLISION_FILTER_KEYS[kind]}): {state}",
                                flush=True,
                            )
                        key_polls[f"KIND_{kind}"] += 1
                    kind_key_was_down[kind] = down
            elif collision_controls_active:
                nearest_down = pressed("I")
                if nearest_down and not collision_nearest_was_down:
                    collision_nearest_only = not collision_nearest_only
                    key_polls["I"] += 1
                    collision_filter_changed = True
                    last_reported_triangle = None
                    state = "ONE FACE" if collision_nearest_only else "NEARBY SET"
                    print(f"collision selection: {state}", flush=True)
                collision_nearest_was_down = nearest_down
                for key in range(10):
                    down = kind_key_pressed(key)
                    if down and not collision_key_was_down[key]:
                        if key == 0:
                            collision_visible = not collision_visible
                            client.write(
                                collision_data_address,
                                int(collision_visible),
                                32,
                            )
                            state = "ON" if collision_visible else "OFF"
                            print(f"collision: {state}", flush=True)
                        else:
                            collision_orientation = COLLISION_FILTER_KEYS[key]
                            collision_filter_changed = True
                            last_reported_triangle = None
                            print(
                                f"collision filter: {collision_orientation}",
                                flush=True,
                            )
                    collision_key_was_down[key] = down
            else:
                for kind in range(KIND_SLOT_COUNT):
                    down = kind_key_pressed(kind)
                    if down and not kind_key_was_down[kind]:
                        kind_visibility[kind] = not kind_visibility[kind]
                        key_polls[f"KIND_{kind}"] += 1
                        if args.kind_visibility_address:
                            client.write(
                                args.kind_visibility_address + kind,
                                int(kind_visibility[kind]),
                                8,
                            )
                        state = "ON" if kind_visibility[kind] else "OFF"
                        print(f"kind {kind}: {state}", flush=True)
                    kind_key_was_down[kind] = down
            now = time.monotonic()
            loop_gap = now - previous
            if args.trace_loop_timing:
                loop_timing_iterations += 1
                loop_timing_max_gap = max(loop_timing_max_gap, loop_gap)
                if loop_gap > 1.0 / 60.0:
                    loop_timing_over_16ms += 1
                if loop_gap > 1.0 / 30.0:
                    loop_timing_over_33ms += 1
            dt = min(loop_gap, 0.05)
            previous = now
            if (
                args.trace_camera_race
                and freecam_enabled
                and camera_available
                and now >= camera_race_next_render_sample
            ):
                observed_block, render_matrix = client.read_blocks(
                    [
                        (camera_transform, 0x20),
                        (
                            render_camera + RW_CAMERA_MATRIX_OFFSET,
                            0x40,
                        ),
                    ]
                )
                observed_position = struct.unpack_from("<3f", observed_block)
                desired_position = struct.unpack_from("<3f", last_written_block)
                logical_error = math.dist(observed_position, desired_position)
                camera_race_reads += 1
                camera_race_max_logical_error = max(
                    camera_race_max_logical_error, logical_error
                )
                if logical_error > 0.05:
                    camera_race_logical_overrides += 1
                    if camera_race_logical_reports < 12:
                        active_object = client.read(CAMERA_OBJECT_POINTER, 32)
                        elapsed = now - camera_race_start
                        print(
                            "camera_race logical_override "
                            f"t={elapsed:.3f}s error={logical_error:.3f} "
                            f"object=0x{active_object:08X} "
                            f"desired=({desired_position[0]:.3f}, "
                            f"{desired_position[1]:.3f}, "
                            f"{desired_position[2]:.3f}) "
                            f"observed=({observed_position[0]:.3f}, "
                            f"{observed_position[1]:.3f}, "
                            f"{observed_position[2]:.3f})",
                            flush=True,
                        )
                        camera_race_logical_reports += 1
                render_position = render_camera_world_position_from_matrix(
                    render_matrix
                )
                camera_race_render_reads += 1
                if camera_race_last_render_position is not None:
                    render_step = math.dist(
                        render_position, camera_race_last_render_position
                    )
                    camera_race_max_render_step = max(
                        camera_race_max_render_step, render_step
                    )
                    if render_step > 2.0:
                        camera_race_render_jumps += 1
                        if camera_race_render_reports < 24:
                            elapsed = now - camera_race_start
                            print(
                                "camera_race render_jump "
                                f"t={elapsed:.3f}s step={render_step:.3f} "
                                f"render=({render_position[0]:.3f}, "
                                f"{render_position[1]:.3f}, "
                                f"{render_position[2]:.3f})",
                                flush=True,
                            )
                            camera_race_render_reports += 1
                camera_race_last_render_position = render_position
                camera_race_next_render_sample = now + 1.0 / 60.0
            if not freecam_enabled:
                try:
                    camera_transform = resolve_camera_transform(
                        client,
                        camera_object=camera_object,
                    )
                    camera_available = True
                    normal_raw = client.read_block(
                        camera_transform, 0x60, progress=False
                    )
                    normal_values = list(
                        struct.unpack_from("<8f", normal_raw)
                    )
                    position = normal_values[:3]
                    position_w = normal_values[3]
                    quaternion = normalize_quaternion(
                        tuple(normal_values[4:8])
                    )
                    frustum_slopes = struct.unpack_from(
                        "<2f", normal_raw, 0x4C
                    )
                except RuntimeError:
                    camera_available = False
            if args.trace_render_space and now >= next_render_sample:
                render_position = render_camera_world_position(
                    client, render_camera
                )
                render_samples.append((last_written_position.copy(), render_position))
                next_render_sample = now + 0.05
            speed = args.speed
            if controller_input is not None and controller_input.controller_mode:
                speed *= controller_input.speed_factor
            if pressed("SHIFT"):
                speed *= 4.0
            if pressed("CTRL"):
                speed *= 0.25

            yaw = 0.0
            pitch = 0.0
            look_step = math.radians(args.look_speed) * dt
            if controller_input is not None and controller_input.controller_mode and freecam_enabled:
                yaw -= controller_input.look[0] * look_step
                pitch -= controller_input.look[1] * look_step
            if freecam_enabled and pressed("H"):
                key_polls["H"] += 1
                yaw += look_step
            if freecam_enabled and pressed("K"):
                key_polls["K"] += 1
                yaw -= look_step
            if freecam_enabled and pressed("U"):
                key_polls["U"] += 1
                pitch += look_step
            if freecam_enabled and pressed("J"):
                key_polls["J"] += 1
                pitch -= look_step

            foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            center = window_center(game_window)
            if (
                freecam_enabled
                and not (controller_input is not None and controller_input.controller_mode)
                and game_window
                and foreground == game_window
                and center is not None
            ):
                cursor = Point()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
                if mouse_engaged:
                    delta_x = cursor.x - center[0]
                    delta_y = cursor.y - center[1]
                    if abs(delta_x) < 1000 and abs(delta_y) < 1000:
                        yaw -= math.radians(delta_x * args.mouse_sensitivity)
                        pitch -= math.radians(delta_y * args.mouse_sensitivity)
                ctypes.windll.user32.SetCursorPos(*center)
                mouse_engaged = True
            else:
                mouse_engaged = False

            if yaw:
                yaw_rotation = quaternion_axis_angle((0.0, 0.0, 1.0), yaw)
                quaternion = normalize_quaternion(
                    quaternion_multiply(yaw_rotation, quaternion)
                )
            if pitch:
                pitch_rotation = quaternion_axis_angle((1.0, 0.0, 0.0), pitch)
                candidate = normalize_quaternion(
                    quaternion_multiply(quaternion, pitch_rotation)
                )
                candidate_forward = rotate(candidate, (0.0, 1.0, 0.0))
                if abs(candidate_forward[2]) < 0.995:
                    quaternion = candidate

            right = normalize(rotate(quaternion, (1.0, 0.0, 0.0)))
            forward = normalize(rotate(quaternion, (0.0, 1.0, 0.0)))

            movement = [0.0, 0.0, 0.0]
            for key, axis, sign in (
                ("W", forward, 1.0),
                ("S", forward, -1.0),
                ("D", right, 1.0),
                ("A", right, -1.0),
                ("E", world_up, 1.0),
                ("Q", world_up, -1.0),
            ):
                if freecam_enabled and pressed(key):
                    key_polls[key] += 1
                    for index in range(3):
                        movement[index] += axis[index] * sign

            auto_axis = {
                "none": None,
                "right": right,
                "forward": forward,
                "up": world_up,
            }[args.auto]
            if controller_input is not None and controller_input.controller_mode and freecam_enabled:
                for index in range(3):
                    movement[index] += (right[index] * controller_input.move[0]
                                        + forward[index] * controller_input.move[1]
                                        + world_up[index] * controller_input.move[2])
            if freecam_enabled and auto_axis is not None:
                for index in range(3):
                    movement[index] += auto_axis[index]

            length = math.sqrt(sum(component * component for component in movement))
            if length:
                for index in range(3):
                    position[index] += movement[index] / max(1.0, length) * speed * dt

            block = struct.pack(
                "<8f", *position, position_w, *quaternion
            )
            if native_camera is not None:
                native_camera.update(position, quaternion, freecam_enabled and camera_available)
            if freecam_enabled and camera_available:
                if native_camera is None:
                    client.write_block(camera_transform, block)
                last_written_block = block
            if args.camera_override_code_address:
                if freecam_enabled and now >= next_camera_override_update:
                    render_matrix = build_freecam_render_matrix(
                        position,
                        quaternion,
                        frustum_slopes,
                    )
                    client.write_block(
                        args.camera_override_code_address
                        + CAMERA_OVERRIDE_MATRIX_OFFSET,
                        render_matrix,
                    )
                    if not camera_override_enabled:
                        client.write(
                            args.camera_override_code_address
                            + CAMERA_OVERRIDE_FLAG_OFFSET,
                            1,
                            32,
                        )
                        camera_override_enabled = True
                    camera_override_updates += 1
                    next_camera_override_update = now + 1.0 / 60.0
                elif not freecam_enabled and camera_override_enabled:
                    client.write(
                        args.camera_override_code_address
                        + CAMERA_OVERRIDE_FLAG_OFFSET,
                        0,
                        32,
                    )
                    camera_override_enabled = False
            last_written_position = position.copy()
            if (
                player_controller is not None
                and player_controller.attached
                and freecam_enabled
                and now >= next_player_update
            ):
                try:
                    player_target = player_controller.write(
                        position,
                        forward,
                    )
                except RuntimeError as error:
                    begin_world_transition(str(error))
                    continue
                next_player_update = now + 1.0 / args.player_update_hz
            if (
                collision_controls_active
                and (
                    collision_filter_changed
                    or now >= next_collision_update
                )
            ):
                collision_update_started = time.perf_counter()
                assert collision_vertices is not None
                assert collision_triangles is not None
                if collision_nearest_only:
                    collision_rows = collision_probe.select_nearest_triangle(
                        collision_vertices,
                        collision_triangles,
                        tuple(position),
                        collision_orientation,
                        args.collision_wall_normal_z,
                    )
                else:
                    collision_rows = collision_probe.select_triangles(
                        collision_vertices,
                        collision_triangles,
                        tuple(position),
                        args.collision_radius,
                        args.collision_limit,
                        collision_orientation,
                        args.collision_wall_normal_z,
                        spatial_index=collision_spatial_index,
                    )
                collision_probe.update_data_block(
                    client,
                    collision_data_address,
                    collision_rows,
                    collision_orientation,
                    enabled=collision_visible,
                )
                collision_updates += 1
                collision_selected_count = len(collision_rows)
                if collision_nearest_only and collision_rows:
                    triangle_index = collision_rows[0]["index"]
                    if triangle_index != last_reported_triangle:
                        print(
                            "face probe: "
                            + collision_probe.describe_triangle(
                                collision_rows[0],
                                args.collision_wall_normal_z,
                            ),
                            flush=True,
                        )
                        last_reported_triangle = triangle_index
                next_collision_update = (
                    now + 1.0 / args.collision_update_hz
                )
                if args.trace_loop_timing:
                    collision_update_timings.append(
                        time.perf_counter() - collision_update_started
                    )
            if (
                simultaneous_requested
                and (
                    collision_filter_changed
                    or now >= next_collision_update
                )
            ):
                collision_update_started = time.perf_counter()
                assert simultaneous_probe is not None
                assert collision_vertices is not None
                assert collision_triangles is not None
                terrain_rows = simultaneous_probe.select_terrain_rows(
                    collision_vertices,
                    collision_triangles,
                    tuple(position),
                    args.collision_radius,
                    args.collision_limit,
                    terrain_visibility,
                    args.collision_wall_normal_z,
                    spatial_index=collision_spatial_index,
                    climb_classes=climb_classes if climb_mode else None,
                )
                terrain_records = simultaneous_probe.build_terrain_records(
                    terrain_rows,
                    collision_triangles,
                    simultaneous_vertex_pointer,
                    terrain_visibility,
                    args.collision_wall_normal_z,
                    climb_classes=climb_classes if climb_mode else None,
                )
                collision_selected_count = (
                    simultaneous_probe.update_terrain_records(
                        client,
                        args.simultaneous_code_address,
                        terrain_records,
                        enabled=terrain_master_visible,
                        record_address=(
                            args.simultaneous_data_address
                            + simultaneous_probe.EXTENDED_TERRAIN_RECORD_OFFSET
                            if obb_layout_supported
                            else None
                        ),
                    )
                )
                collision_updates += 1
                next_collision_update = (
                    now + 1.0 / args.collision_update_hz
                )
                if args.trace_loop_timing:
                    collision_update_timings.append(
                        time.perf_counter() - collision_update_started
                    )
            if (
                player_debug_visible
                and player_controller is not None
                and player_debug_sampler is not None
                and now >= next_player_debug_update
            ):
                try:
                    _debug_human, player_debug_position = (
                        read_player_position(
                            client,
                            player_controller.human,
                        )
                    )
                    player_debug_shapes = player_debug_sampler.sample(
                        player_debug_position
                    )
                except RuntimeError as error:
                    begin_world_transition(str(error))
                    continue
                if simultaneous_probe is not None:
                    (
                        debug_points,
                        debug_lines,
                        debug_line_count,
                    ) = simultaneous_probe.build_player_debug_data(
                        player_debug_shapes
                    )
                    simultaneous_probe.update_player_debug_data(
                        client,
                        args.simultaneous_code_address,
                        debug_points,
                        debug_lines,
                        debug_line_count,
                        enabled=True,
                    )
                signature = tuple(
                    (
                        shape.kind,
                        shape.address,
                        shape.flags,
                        round(shape.radius, 5),
                    )
                    for shape in player_debug_shapes
                )
                if signature != last_player_debug_signature:
                    descriptions = [
                        (
                            f"type={shape.kind} "
                            f"addr=0x{shape.address:08X} "
                            f"flags=0x{shape.flags:08X} "
                            f"radius={shape.radius:.4f} "
                            f"A={tuple(round(value, 4) for value in shape.first)} "
                            f"B={tuple(round(value, 4) for value in shape.second)}"
                        )
                        for shape in player_debug_shapes
                    ]
                    print(
                        "player bounds: "
                        + (" | ".join(descriptions) or "none"),
                        flush=True,
                    )
                    last_player_debug_signature = signature
                principal = next(
                    (
                        shape
                        for shape in player_debug_shapes
                        if shape.kind == 3
                    ),
                    None,
                )
                player_debug_radius = (
                    principal.radius if principal is not None else 1.0
                )
                next_player_debug_update = now + 1.0 / 60.0
            # B deliberately shows the raw type-1 OBB inventory.  C must not
            # equate that inventory with gameplay climbability: it selects
            # only OBBs owned by confirmed native climb families (Car is the
            # currently validated dynamic family).
            obb_overlay_kind = (
                "climb"
                if args.climb_object_support
                and climb_mode
                and terrain_master_visible
                else ("debug" if object_debug_visible else None)
            )
            if (
                obb_layout_supported
                and player_debug_sampler is not None
                and simultaneous_probe is not None
                and now >= next_object_debug_update
                and (
                    obb_overlay_kind is not None
                    or last_obb_overlay_kind is not None
                )
            ):
                if obb_overlay_kind is None:
                    object_debug_shapes = []
                    debug_records = b""
                    debug_obb_count = 0
                else:
                    object_debug_shapes = (
                        player_debug_sampler.sample_nearby_climb_obbs(
                            tuple(position),
                            maximum_distance=args.collision_radius,
                            limit=simultaneous_probe.OBB_CAPACITY,
                        )
                        if obb_overlay_kind == "climb"
                        else player_debug_sampler.sample_nearby_obbs(
                            tuple(position),
                            maximum_distance=args.collision_radius,
                            limit=simultaneous_probe.OBB_CAPACITY,
                        )
                    )
                    debug_records, debug_obb_count = (
                        simultaneous_probe.build_obb_debug_records(
                            object_debug_shapes
                        )
                    )
                simultaneous_probe.update_obb_debug_records(
                    client,
                    args.simultaneous_code_address,
                    args.simultaneous_data_address,
                    debug_records,
                    debug_obb_count,
                    enabled=obb_overlay_kind is not None,
                    color=(
                        simultaneous_probe.CLIMB_DYNAMIC_OBB_COLOR
                        if obb_overlay_kind == "climb"
                        else simultaneous_probe.OBJECT_OBB_COLOR
                    ),
                )
                signature = (
                    obb_overlay_kind,
                    tuple(
                        (
                            shape.address,
                            shape.owner,
                            shape.flags,
                            tuple(
                                round(value, 4)
                                for value in shape.half_extents
                            ),
                        )
                        for shape in object_debug_shapes
                    ),
                )
                if signature != last_object_debug_signature:
                    if obb_overlay_kind is None:
                        description = "hidden"
                    elif not object_debug_shapes:
                        description = "none nearby"
                    else:
                        nearest = object_debug_shapes[0]
                        description = (
                            f"shown={len(object_debug_shapes)}/"
                            f"{simultaneous_probe.OBB_CAPACITY} "
                            f"nearest=0x{nearest.address:08X} "
                            f"distance={nearest.distance:.3f} "
                            f"farthest={object_debug_shapes[-1].distance:.3f}"
                        )
                    print(
                        f"dynamic {obb_overlay_kind or 'OBB'} bounds: "
                        f"{description}",
                        flush=True,
                    )
                    last_object_debug_signature = signature
                last_obb_overlay_kind = obb_overlay_kind
                next_object_debug_update = now + 0.5
            if (
                gang_broadcast is not None
                and now >= next_gang_telemetry_update
            ):
                try:
                    assert gang_reader is not None
                    assert gang_snapshot_to_packet is not None
                    gang_camera = (
                        tuple(position),
                        tuple(right),
                        tuple(forward),
                        tuple(normalize(rotate(quaternion, (0.0, 0.0, 1.0)))),
                        float(frustum_slopes[0]),
                        float(frustum_slopes[1]),
                    )
                    gang_snapshot = gang_reader.read(
                        client,
                        camera_override=gang_camera,
                    )
                    gang_broadcast.sendto(
                        gang_snapshot_to_packet(gang_snapshot),
                        ("127.0.0.1", args.gang_broadcast_port),
                    )
                    last_gang_telemetry_error = ""
                except (OSError, RuntimeError, ValueError, struct.error) as error:
                    message = str(error)
                    if message != last_gang_telemetry_error:
                        print(
                            f"gang telemetry temporarily unavailable: {message}",
                            flush=True,
                        )
                        last_gang_telemetry_error = message
                next_gang_telemetry_update = (
                    time.monotonic() + 1.0 / args.gang_telemetry_hz
                )
            if broadcast is not None and now >= next_broadcast_update:
                if (
                    destroyed_cp4_sampler is not None
                    and now >= next_destroyed_cp4_update
                ):
                    destroyed_cp4_packet = destroyed_cp4_sampler.sample(client)
                    next_destroyed_cp4_update = (
                        now + 1.0 / args.destroyed_cp4_telemetry_hz
                    )
                if now >= next_broadcast_player_update:
                    from player_noclip import read_player_position

                    try:
                        refresh_player_identity = (
                            player_controller is None
                            and (
                                not broadcast_player_human
                                or now >= next_broadcast_player_refresh
                            )
                        )
                        if player_controller is not None:
                            broadcast_player_human = player_controller.human
                        elif refresh_player_identity:
                            broadcast_player_human = 0
                        (
                            broadcast_player_human,
                            broadcast_player_position,
                        ) = read_player_position(
                            client,
                            broadcast_player_human or None,
                        )
                        next_broadcast_player_update = now + 1.0 / 20.0
                        if refresh_player_identity:
                            next_broadcast_player_refresh = now + 1.0
                    except RuntimeError:
                        broadcast_player_human = 0
                        broadcast_player_position = None
                        next_broadcast_player_update = now + 0.25
                        next_broadcast_player_refresh = now + 0.25
                broadcast_visibility = (
                    kind_visibility
                    if (
                        not combined_requested
                        or combined_renderer_mode == "triggers"
                    )
                    else [False] * KIND_SLOT_COUNT
                )
                debug_suffix = b""
                if simultaneous_requested:
                    trigger_mask = sum(
                        1 << kind
                        for kind in range(4)
                        if kind_visibility[kind]
                    )
                    terrain_mask = sum(
                        1 << (kind - 4)
                        for kind in range(4, 10)
                        if terrain_visibility[kind]
                    )
                    attached = bool(
                        player_controller is not None
                        and player_controller.attached
                        and freecam_enabled
                    )
                    god_enabled = bool(
                        player_controller is not None
                        and player_controller.god_mode
                    )
                    debug_flags = (
                        int(terrain_master_visible)
                        | (int(freecam_enabled) << 1)
                        | (int(attached) << 2)
                        | (int(god_enabled) << 3)
                        | (int(player_debug_visible) << 4)
                        | ((flag_mode & 0x03) << 5)
                        | (int(climb_mode) << 7)
                    )
                    debug_suffix = DEBUG_STATE_HEADER.pack(
                        DEBUG_STATE_MAGIC,
                        DEBUG_STATE_VERSION,
                        trigger_mask,
                        terrain_mask,
                        debug_flags,
                        (
                            collision_selected_count
                            if terrain_master_visible
                            else 0
                        ),
                        simultaneous_probe.TERRAIN_CAPACITY,
                        *player_debug_position,
                        player_debug_radius,
                        len(player_debug_shapes),
                    ) + b"".join(
                        DEBUG_SHAPE_RECORD.pack(
                            shape.kind,
                            *shape.first,
                            *shape.second,
                            shape.radius,
                        )
                        for shape in player_debug_shapes
                    )
                position_suffix = POSITION_STATE_HEADER.pack(
                    POSITION_STATE_MAGIC,
                    POSITION_STATE_VERSION,
                    int(broadcast_player_position is not None),
                    *(broadcast_player_position or (0.0, 0.0, 0.0)),
                )
                volume_selection_suffix = VOLUME_SELECTION_HEADER.pack(
                    VOLUME_SELECTION_MAGIC,
                    VOLUME_SELECTION_VERSION,
                    int(volume_solo_enabled),
                    volume_selection_index,
                    args.volume_count,
                )
                broadcast.sendto(
                    block
                    + struct.pack("<2f", *frustum_slopes)
                    + debug_suffix
                    + position_suffix
                    + volume_selection_suffix
                    + destroyed_cp4_packet
                    + KIND_VISIBILITY_MAGIC
                    + bytes(broadcast_visibility),
                    ("127.0.0.1", args.broadcast_port),
                )
                next_broadcast_update = now + 1.0 / 60.0
            writes += 1
            time.sleep(max(0.0, 1.0 / args.hz))
    finally:
        f10_watchdog_shutdown.set()
        f10_watchdog.join(timeout=0.25)
        _controller_input = None
        if controller_input is not None:
            try:
                controller_input.pad.close()
            except Exception as error:
                print(f'CRITICAL: Pad 1 restoration failed: {error}', flush=True)
        if args.return_to_follow_camera and not transition_active:
            try:
                from follow_camera import handoff_to_follow
                handoff_to_follow(client)
            except Exception as error:
                print(f'WARNING: FollowCamera handoff unavailable: {error}', flush=True)
        if native_camera is not None:
            try:
                native_camera.close()
            except Exception as error:
                print(f'CRITICAL: native camera restoration failed: {error}', flush=True)
            print(f"native_camera_publications={native_camera.publications}", flush=True)
        if destroyed_ai_controller is not None and not transition_active:
            try:
                if destroyed_cp4_sampler is not None:
                    destroyed_cp4_sampler.sample(client)
                restored = destroyed_ai_controller.restore(
                    client,
                    (
                        destroyed_cp4_sampler.latest_snapshot
                        if destroyed_cp4_sampler is not None
                        else None
                    ),
                )
                if restored:
                    print(
                        f"DESTROYED CP4 AI restored on exit: {restored}",
                        flush=True,
                    )
            except Exception as error:
                print(
                    f"WARNING: could not restore AI: {error}",
                    flush=True,
                )
        if (
            world_time is not None
            and world_time.active
            and not transition_active
        ):
            try:
                restored = world_time.disable()
                print(
                    "world time restored on exit "
                    f"(timestep={restored:.9g})",
                    flush=True,
                )
            except Exception as error:
                print(
                    f"WARNING: could not restore world timestep: {error}",
                    flush=True,
                )
        if args.camera_override_code_address and not transition_active:
            try:
                client.write(
                    args.camera_override_code_address
                    + CAMERA_OVERRIDE_FLAG_OFFSET,
                    0,
                    32,
                )
                camera_override_enabled = False
            except Exception as error:
                print(
                    f"WARNING: could not disable native camera override: {error}",
                    flush=True,
                )
        if camera_override_begin_hook_installed:
            try:
                client.write(
                    CAMERA_BEGIN_UPDATE_HOOK,
                    CAMERA_BEGIN_UPDATE_ORIGINAL,
                    32,
                )
                if (
                    client.read(CAMERA_BEGIN_UPDATE_HOOK, 32)
                    != CAMERA_BEGIN_UPDATE_ORIGINAL
                ):
                    raise RuntimeError(
                        "camera BeginUpdate hook restoration did not verify"
                    )
                camera_override_begin_hook_installed = False
            except Exception as error:
                print(
                    "WARNING: could not restore camera BeginUpdate hook: "
                    f"{error}",
                    flush=True,
                )
        if (
            not transition_active
            and combined_requested
            and combined_renderer_mode != "triggers"
        ):
            try:
                install_combined_renderer("triggers", tuple(position))
                combined_renderer_mode = "triggers"
            except Exception as error:
                print(
                    f"WARNING: could not restore trigger renderer mode: {error}",
                    flush=True,
                )
        if mouse_engaged:
            ctypes.windll.user32.SetCursorPos(original_cursor.x, original_cursor.y)
        if simultaneous_probe is not None and not transition_active:
            try:
                simultaneous_probe.update_player_debug_data(
                    client,
                    args.simultaneous_code_address,
                    b"",
                    b"",
                    0,
                    enabled=False,
                )
                if obb_layout_supported:
                    simultaneous_probe.update_obb_debug_records(
                        client,
                        args.simultaneous_code_address,
                        args.simultaneous_data_address,
                        b"",
                        0,
                        enabled=False,
                    )
            except Exception as error:
                print(
                    f"WARNING: could not disable debug bounds: {error}",
                    flush=True,
                )
        if broadcast is not None:
            broadcast.close()
        if gang_broadcast is not None:
            gang_broadcast.close()
        if air_stand_controller is not None and not transition_active:
            try:
                air_stand_controller.close()
                air_stand_controller = None
            except Exception as error:
                print(
                    "WARNING: could not restore player air-stand hooks: "
                    f"{error}",
                    flush=True,
                )
        if player_controller is not None and not transition_active:
            try:
                player_controller.close()
            except Exception as error:
                print(
                    f"WARNING: could not restore player safety/state: {error}",
                    flush=True,
                )
        try:
            try:
                if visibility_hotkeys is not None:
                    visibility_hotkeys.close()
            finally:
                if panel_hotkeys is not None:
                    panel_hotkeys.close()
        finally:
            client.close()

    print(
        "probe_complete "
        f"start=({start_position[0]:.4f}, {start_position[1]:.4f}, "
        f"{start_position[2]:.4f}) "
        f"position=({position[0]:.4f}, {position[1]:.4f}, {position[2]:.4f}) "
        f"writes={writes} key_polls={key_polls} "
        f"collision_updates={collision_updates} "
        f"collision_selected={collision_selected_count} "
        f"player_writes={player_controller.writes if player_controller else 0} "
        f"gravity_resets="
        f"{player_controller.gravity_resets if player_controller else 0} "
        f"player_target={player_target} "
        f"camera_override_updates={camera_override_updates} "
        f"camera_switches={camera_switches} "
        f"camera_recoveries={camera_recoveries} "
        f"camera_resolution_failures={camera_resolution_failures}"
    )
    if render_samples:
        print(f"render_space_samples={len(render_samples)}")
        try:
            coefficients, rms = fit_affine_space(render_samples)
            for axis, row in zip("XYZ", coefficients):
                print(
                    f"render_{axis}=({row[0]:.9f})*game_x+"
                    f"({row[1]:.9f})*game_y+({row[2]:.9f})*game_z+"
                    f"({row[3]:.9f})"
                )
            print(f"render_space_rms={rms:.6f}")
        except ValueError as error:
            print(f"render_space_fit_failed={error}")
    if args.trace_camera_race:
        print(
            "camera_race_summary "
            f"logical_reads={camera_race_reads} "
            f"logical_overrides={camera_race_logical_overrides} "
            f"max_logical_error={camera_race_max_logical_error:.6f} "
            f"render_reads={camera_race_render_reads} "
            f"render_jumps={camera_race_render_jumps} "
            f"max_render_step={camera_race_max_render_step:.6f}"
        )
    if args.trace_loop_timing:
        if collision_update_timings:
            collision_average = (
                sum(collision_update_timings)
                / len(collision_update_timings)
            )
            collision_maximum = max(collision_update_timings)
        else:
            collision_average = 0.0
            collision_maximum = 0.0
        print(
            "loop_timing_summary "
            f"iterations={loop_timing_iterations} "
            f"gaps_over_16ms={loop_timing_over_16ms} "
            f"gaps_over_33ms={loop_timing_over_33ms} "
            f"max_gap_ms={loop_timing_max_gap * 1000.0:.3f} "
            f"collision_updates={len(collision_update_timings)} "
            f"collision_avg_ms={collision_average * 1000.0:.3f} "
            f"collision_max_ms={collision_maximum * 1000.0:.3f}"
        )
    return WORLD_TRANSITION_EXIT_CODE if transition_handoff else 0


if __name__ == "__main__":
    raise SystemExit(main())
