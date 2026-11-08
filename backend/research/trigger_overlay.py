#!/usr/bin/env python3
"""Transparent 3D overlay for extracted The Warriors volumes and flags."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import socket
import struct
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

from freecam_probe import resolve_camera_transform
from pine_client import PineClient


COLOR_KEY = "#010203"
NATIVE_SCREEN_WIDTH = 640.0
NATIVE_SCREEN_HEIGHT = 448.0
VK_R = 0x52
VK_F = 0x46
KIND_SLOT_COUNT = 10
KIND_VISIBILITY_MAGIC = b"TWKV"
DEBUG_STATE_MAGIC = b"TWDB"
DEBUG_STATE_HEADER = struct.Struct("<4sBBBBHH4fH")
DEBUG_SHAPE_RECORD = struct.Struct("<B3x7f")
POSITION_STATE_MAGIC = b"TWPS"
POSITION_STATE_HEADER = struct.Struct("<4sBB2x3f")
VOLUME_SELECTION_MAGIC = b"TWVS"
VOLUME_SELECTION_HEADER = struct.Struct("<4sBBHH")
TERRAIN_NAMES = {
    4: "WALK",
    5: "SLOPE",
    6: "CEILING",
    7: "EXCLUDED",
    8: "COMMON",
    9: "2-SIDED",
}
PLAYER_SHAPE_NAMES = {
    2: "SPHERE",
    3: "MAIN",
    4: "CAPSULE",
}
PLAYER_SHAPE_COLORS = {
    2: "#ff5151",
    3: "#48e8ff",
    4: "#ffae3d",
}
FLAG_TRIGGER_COLOR = "#ff55d7"
FLAG_POINT_COLOR = "#bca7ff"
FLAG_MODE_TRIGGER = 0
FLAG_MODE_ALL = 1
FLAG_MODE_OFF = 2
FLAG_MODE_LABELS = {
    FLAG_MODE_TRIGGER: "TRIGGERS",
    FLAG_MODE_ALL: "ALL",
    FLAG_MODE_OFF: "OFF",
}


class Point(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))


class Rect(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


def rotate_quaternion(q: tuple[float, ...], v: tuple[float, ...]) -> tuple[float, float, float]:
    x, y, z, w = q
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))


def kind_key_pressed(kind: int) -> bool:
    """Accept both the number row and the numeric keypad."""
    user32 = ctypes.windll.user32
    return bool(
        user32.GetAsyncKeyState(0x30 + kind) & 0x8000
        or user32.GetAsyncKeyState(0x60 + kind) & 0x8000
    )


def find_game_window() -> int:
    ctypes.windll.user32.FindWindowW.restype = ctypes.c_void_p
    return int(ctypes.windll.user32.FindWindowW(None, "The Warriors") or 0)


def game_window_has_focus(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    foreground = int(user32.GetForegroundWindow() or 0)
    return bool(hwnd and foreground == hwnd)


def viewport(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = Rect()
    origin = Point(0, 0)
    if not hwnd or not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    if not ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    # In PCSX2's current windowed layout the Qt menubar and statusbar are part
    # of the client area. Fullscreen has no such insets.
    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    fullscreen = width >= screen_w - 4 and height >= screen_h - 4
    top_inset = 0 if fullscreen else 21
    bottom_inset = 0 if fullscreen else 25
    return origin.x, origin.y + top_inset, width, height - top_inset - bottom_inset


def volume_center(volume: dict) -> tuple[float, float, float]:
    # The Lua vector is the unrotated minimum corner, not the midpoint.  The
    # runtime object stores origin + size / 2 as its actual rotation center.
    origin = volume.get("origin", volume.get("center"))
    if origin is None:
        raise ValueError(f"Volume has no origin: {volume.get('name', '<unnamed>')}")
    return tuple(origin[index] + volume["size"][index] * 0.5 for index in range(3))


def box_corners(
    volume: dict,
    extent_scale: float = 0.5,
) -> list[tuple[float, float, float]]:
    cx, cy, cz = volume_center(volume)
    # AddVolumeBox receives full dimensions. Corners are +/- half of each size
    # component around the midpoint derived above.
    ex, ey, ez = (component * extent_scale for component in volume["size"])
    rotation = volume.get("rotation_xy") or [1.0, 0.0, 0.0, 1.0]
    a, b, c, d = rotation
    corners = []
    for index in range(8):
        x = ex if index & 1 else -ex
        y = ey if index & 2 else -ey
        z = ez if index & 4 else -ez
        corners.append((cx + a * x + b * y, cy + c * x + d * y, cz + z))
    return corners


EDGES = tuple(
    (index, index ^ bit)
    for index in range(8)
    for bit in (1, 2, 4)
    if index < (index ^ bit)
)


KIND_HEX_COLORS = {
    0: "#ff554d",
    1: "#55ff78",
    2: "#ffe24a",
    3: "#35d7ff",
}


def color_for(volume: dict) -> str:
    """Match the in-game renderer's palette, based exclusively on kind."""
    return KIND_HEX_COLORS.get(volume["kind"], "#ffffff")


def order_by_current_camera(volumes: list[dict], port: int) -> list[dict]:
    """Reproduce all_volumes_probe.py's nearest-first installation order."""
    client = PineClient(port=port)
    try:
        transform = resolve_camera_transform(client)
        camera_position = struct.unpack_from(
            "<3f", client.read_block(transform, 16, progress=False)
        )
    finally:
        client.close()
    return sorted(
        volumes,
        key=lambda volume: math.dist(camera_position, volume_center(volume)),
    )


def point_in_volume(
    point: tuple[float, float, float],
    volume: dict,
    tolerance: float = 0.08,
) -> bool:
    """Test a gameplay-space point against one possibly rotated Lua box."""
    center_x, center_y, center_z = volume_center(volume)
    delta_x = point[0] - center_x
    delta_y = point[1] - center_y
    delta_z = point[2] - center_z
    rotation = volume.get("rotation_xy") or [1.0, 0.0, 0.0, 1.0]
    a, b, c, d = rotation
    determinant = a * d - b * c
    if abs(determinant) < 1.0e-8:
        return False
    local_x = (d * delta_x - b * delta_y) / determinant
    local_y = (-c * delta_x + a * delta_y) / determinant
    extent_x, extent_y, extent_z = (
        component * 0.5 for component in volume["size"]
    )
    return (
        abs(local_x) <= extent_x + tolerance
        and abs(local_y) <= extent_y + tolerance
        and abs(delta_z) <= extent_z + tolerance
    )


def volume_size(volume: dict) -> float:
    return math.prod(abs(component) for component in volume["size"])


def flag_sphere_radii(flag: dict) -> list[float]:
    """Return unique positive statically recovered radii for one flag."""
    return sorted(
        {
            float(use["radius"])
            for use in flag.get("sphere_uses", [])
            if math.isfinite(float(use.get("radius", 0.0)))
            and float(use["radius"]) > 0.0
        }
    )


def point_in_flag_sphere(
    point: tuple[float, float, float],
    flag: dict,
) -> float | None:
    """Return the smallest containing trigger radius, if any."""
    distance = math.dist(point, tuple(flag["position"]))
    for radius in flag_sphere_radii(flag):
        if distance <= radius:
            return radius
    return None


class Overlay:
    def __init__(
        self,
        volumes: list[dict],
        flags: list[dict],
        duration: float,
        fov: float,
        port: int,
        camera_state_port: int = 0,
        extent_scale: float = 0.5,
        labels_only: bool = False,
        projected_label_address: int = 0,
        player_position_address: int = 0,
        camera_volume_panel: bool = False,
        native_flag_geometry: bool = False,
        status_x_offset: int = 0,
        ui_panels_state: Path | None = None,
    ):
        self.volumes = volumes
        self.flags = flags
        self.duration = duration
        self.fov = math.radians(fov)
        self.extent_scale = extent_scale
        self.labels_only = labels_only
        self.projected_label_address = projected_label_address
        self.projected_label_size = len(volumes) * 16
        self.player_position_address = player_position_address
        self.camera_volume_panel = camera_volume_panel
        self.native_flag_geometry = native_flag_geometry
        self.status_x_offset = status_x_offset
        self.ui_panels_state = ui_panels_state
        self.ui_panels_summary = ""
        self.ui_panels_next_read = 0.0
        self.overlay_visible = True
        self.toggle_was_down = False
        self.flag_mode = FLAG_MODE_TRIGGER
        self.flag_toggle_was_down = False
        self.kind_visibility = [True] * KIND_SLOT_COUNT
        self.kind_toggle_was_down = [False] * KIND_SLOT_COUNT
        self.volume_selection_index = 0
        self.volume_solo_enabled = False
        self.debug_state: dict | None = None
        self.player_debug_shapes: list[dict] = []
        self.client = None if camera_state_port else PineClient(port=port)
        self.camera_transform = (
            None if self.client is None else resolve_camera_transform(self.client)
        )
        self.camera_socket = None
        self.last_camera_block: bytes | None = None
        self.last_projection_block: bytes | None = None
        self.last_player_position: tuple[float, float, float] | None = None
        if camera_state_port:
            self.camera_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.camera_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, 8192
            )
            self.camera_socket.bind(("127.0.0.1", camera_state_port))
            self.camera_socket.setblocking(False)
        window_deadline = time.monotonic() + 3.0
        self.game_window = find_game_window()
        while not self.game_window and time.monotonic() < window_deadline:
            time.sleep(0.05)
            self.game_window = find_game_window()
        if not self.game_window:
            raise RuntimeError(
                "Could not find the PCSX2 window titled 'The Warriors'"
            )
        self.deadline = time.monotonic() + duration if duration > 0 else math.inf
        self.geometry: tuple[int, int, int, int] | None = None

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg=COLOR_KEY)
        self.root.attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", COLOR_KEY)
        self.canvas = tk.Canvas(
            self.root, bg=COLOR_KEY, highlightthickness=0, borderwidth=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self.make_click_through()

    def make_click_through(self) -> None:
        user32 = ctypes.windll.user32
        hwnd = int(self.root.winfo_id())
        parent = int(user32.GetParent(hwnd) or 0)
        if parent:
            hwnd = parent
        get_style = user32.GetWindowLongPtrW
        set_style = user32.SetWindowLongPtrW
        ex_style = int(get_style(hwnd, -20))
        ex_style |= 0x00000020 | 0x00000080 | 0x00080000 | 0x08000000
        set_style(hwnd, -20, ex_style)

    def camera(self):
        if self.camera_socket is not None:
            candidate = None
            while True:
                try:
                    received, _address = self.camera_socket.recvfrom(65535)
                except BlockingIOError:
                    break
                if len(received) >= 0x20:
                    candidate = received
            if candidate is not None:
                camera_size = 0x28 if len(candidate) >= 0x28 else 0x20
                self.last_camera_block = candidate[:camera_size]
                debug_offset = camera_size
                state_offset = camera_size
                if (
                    len(candidate)
                    >= debug_offset + DEBUG_STATE_HEADER.size
                    and candidate[
                        debug_offset : debug_offset + 4
                    ] == DEBUG_STATE_MAGIC
                ):
                    (
                        _magic,
                        version,
                        trigger_mask,
                        terrain_mask,
                        flags,
                        terrain_count,
                        terrain_capacity,
                        player_x,
                        player_y,
                        player_z,
                        player_radius,
                        shape_count,
                    ) = DEBUG_STATE_HEADER.unpack_from(
                        candidate, debug_offset
                    )
                    shapes_offset = debug_offset + DEBUG_STATE_HEADER.size
                    shapes_end = (
                        shapes_offset
                        + shape_count * DEBUG_SHAPE_RECORD.size
                    )
                    if version == 1 and shapes_end <= len(candidate):
                        shapes = []
                        for shape_index in range(shape_count):
                            values = DEBUG_SHAPE_RECORD.unpack_from(
                                candidate,
                                shapes_offset
                                + shape_index * DEBUG_SHAPE_RECORD.size,
                            )
                            shapes.append(
                                {
                                    "kind": values[0],
                                    "first": tuple(values[1:4]),
                                    "second": tuple(values[4:7]),
                                    "radius": values[7],
                                }
                            )
                        self.debug_state = {
                            "trigger_mask": trigger_mask,
                            "terrain_mask": terrain_mask,
                            "terrain_master": bool(flags & 0x01),
                            "freecam": bool(flags & 0x02),
                            "carry": bool(flags & 0x04),
                            "god": bool(flags & 0x08),
                            "player_debug": bool(flags & 0x10),
                            "climb_mode": bool(flags & 0x80),
                            "flag_mode": (flags >> 5) & 0x03,
                            "terrain_count": terrain_count,
                            "terrain_capacity": terrain_capacity,
                            "player": (player_x, player_y, player_z),
                            "player_radius": player_radius,
                        }
                        recovered_flag_mode = (flags >> 5) & 0x03
                        if recovered_flag_mode in FLAG_MODE_LABELS:
                            self.flag_mode = recovered_flag_mode
                        self.player_debug_shapes = shapes
                        state_offset = shapes_end
                if (
                    len(candidate)
                    >= state_offset + POSITION_STATE_HEADER.size
                    and candidate[state_offset : state_offset + 4]
                    == POSITION_STATE_MAGIC
                ):
                    (
                        _magic,
                        version,
                        flags,
                        player_x,
                        player_y,
                        player_z,
                    ) = POSITION_STATE_HEADER.unpack_from(
                        candidate, state_offset
                    )
                    player_position = (player_x, player_y, player_z)
                    self.last_player_position = (
                        player_position
                        if (
                            version == 1
                            and flags & 0x01
                            and all(
                                math.isfinite(component)
                                for component in player_position
                            )
                        )
                        else None
                    )
                    state_offset += POSITION_STATE_HEADER.size
                if (
                    len(candidate)
                    >= state_offset + VOLUME_SELECTION_HEADER.size
                    and candidate[state_offset : state_offset + 4]
                    == VOLUME_SELECTION_MAGIC
                ):
                    (
                        _magic,
                        version,
                        flags,
                        selected_index,
                        volume_count,
                    ) = VOLUME_SELECTION_HEADER.unpack_from(
                        candidate, state_offset
                    )
                    if (
                        version == 1
                        and volume_count == len(self.volumes)
                        and selected_index < volume_count
                    ):
                        self.volume_selection_index = selected_index
                        self.volume_solo_enabled = bool(flags & 0x01)
                suffix_size = (
                    len(KIND_VISIBILITY_MAGIC) + KIND_SLOT_COUNT
                )
                if (
                    len(candidate) >= camera_size + suffix_size
                    and candidate[-suffix_size:-KIND_SLOT_COUNT]
                    == KIND_VISIBILITY_MAGIC
                ):
                    visibility = candidate[-KIND_SLOT_COUNT:]
                    if all(value in (0, 1) for value in visibility):
                        self.kind_visibility = [
                            bool(value) for value in visibility
                        ]
                projection_end = 0x28 + self.projected_label_size
                if (
                    self.projected_label_address
                    and len(candidate) >= projection_end
                ):
                    self.last_projection_block = candidate[
                        0x28:projection_end
                    ]
            if self.last_camera_block is None:
                return None
            raw = self.last_camera_block
        else:
            assert self.client is not None
            assert self.camera_transform is not None
            raw = self.client.read_block(self.camera_transform, 0x60, progress=False)
        values = struct.unpack_from("<8f", raw)
        position = tuple(values[:3])
        quaternion = tuple(values[4:8])
        right = rotate_quaternion(quaternion, (1.0, 0.0, 0.0))
        forward = rotate_quaternion(quaternion, (0.0, 1.0, 0.0))
        up = rotate_quaternion(quaternion, (0.0, 0.0, 1.0))
        if len(raw) >= 0x60:
            horizontal_slope, vertical_slope = struct.unpack_from("<2f", raw, 0x4C)
        elif len(raw) >= 0x28:
            horizontal_slope, vertical_slope = struct.unpack_from("<2f", raw, 0x20)
        else:
            vertical_slope = math.tan(self.fov * 0.5)
            horizontal_slope = vertical_slope * (5.0 / 3.0)
        return position, right, forward, up, horizontal_slope, vertical_slope

    def projected_labels(self) -> list[tuple[float, float, float, float]] | None:
        if not self.projected_label_address:
            return None
        if self.camera_socket is not None:
            raw = self.last_projection_block
        else:
            assert self.client is not None
            raw = self.client.read_block(
                self.projected_label_address,
                self.projected_label_size,
                progress=False,
            )
        if raw is None or len(raw) != self.projected_label_size:
            return None
        return list(struct.iter_unpack("<4f", raw))

    def poll_toggle(self) -> None:
        if not game_window_has_focus(self.game_window):
            return
        down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_R) & 0x8000)
        if down and not self.toggle_was_down:
            self.overlay_visible = not self.overlay_visible
        self.toggle_was_down = down
        if not self.native_flag_geometry:
            flag_down = bool(
                ctypes.windll.user32.GetAsyncKeyState(VK_F) & 0x8000
            )
            if flag_down and not self.flag_toggle_was_down:
                self.flag_mode = (self.flag_mode + 1) % 3
            self.flag_toggle_was_down = flag_down
        # With a camera UDP feed, freecam_probe owns the numeric key edges and
        # broadcasts the exact state it also writes to the in-game renderer.
        if self.camera_socket is not None:
            return
        for kind in range(KIND_SLOT_COUNT):
            down = kind_key_pressed(kind)
            if down and not self.kind_toggle_was_down[kind]:
                self.kind_visibility[kind] = not self.kind_visibility[kind]
            self.kind_toggle_was_down[kind] = down

    def kind_is_visible(self, volume: dict) -> bool:
        kind = int(volume["kind"])
        return not 0 <= kind < KIND_SLOT_COUNT or self.kind_visibility[kind]

    def volume_is_visible(self, index: int, volume: dict) -> bool:
        if self.volume_solo_enabled:
            return index == self.volume_selection_index
        return self.kind_is_visible(volume)

    def enabled_volume_count(self) -> int:
        return sum(
            self.volume_is_visible(index, volume)
            for index, volume in enumerate(self.volumes)
        )

    def visible_flags(self) -> list[dict]:
        if self.volume_solo_enabled or self.flag_mode == FLAG_MODE_OFF:
            return []
        if self.flag_mode == FLAG_MODE_ALL:
            return self.flags
        return [flag for flag in self.flags if flag_sphere_radii(flag)]

    def flag_filter_summary(self) -> str:
        trigger_count = sum(bool(flag_sphere_radii(flag)) for flag in self.flags)
        solo_suffix = "   HIDDEN IN SOLO MODE" if self.volume_solo_enabled else ""
        return (
            f"F: FLAGS {FLAG_MODE_LABELS[self.flag_mode]}   "
            f"TRIGGER {trigger_count}/{len(self.flags)}{solo_suffix}"
        )

    def kind_filter_summary(self) -> str:
        if self.debug_state is not None:
            mask = int(self.debug_state["trigger_mask"])
            enabled = " ".join(
                str(kind) for kind in range(4) if mask & (1 << kind)
            ) or "-"
            disabled = " ".join(
                str(kind) for kind in range(4) if not mask & (1 << kind)
            ) or "-"
            return (
                f"0-3 TRIGGERS   ON: {enabled}   OFF: {disabled}"
            )
        enabled = " ".join(
            str(kind)
            for kind, visible in enumerate(self.kind_visibility)
            if visible
        ) or "-"
        disabled = " ".join(
            str(kind)
            for kind, visible in enumerate(self.kind_visibility)
            if not visible
        ) or "-"
        return f"0-9: FILTER KIND   ON: {enabled}   OFF: {disabled}"

    def volume_selection_summary(self) -> str:
        if not self.volumes:
            return "N: NO TRIGGER BOXES   M: SOLO OFF"
        selected_index = min(
            self.volume_selection_index,
            len(self.volumes) - 1,
        )
        selected = self.volumes[selected_index]
        solo = "ON" if self.volume_solo_enabled else "OFF"
        return (
            f"N/SHIFT+N: {selected_index + 1}/{len(self.volumes)} "
            f"{selected['name']}   M: SOLO {solo}"
        )

    def debug_status_lines(self) -> list[str]:
        if self.debug_state is None:
            return []
        state = self.debug_state
        terrain_mask = int(state["terrain_mask"])
        if state["climb_mode"]:
            categories = (
                "C CLIMB ON   GREEN:STATIC + LIVE BOUNDS   "
                "CYAN:SPECIAL WOOD"
            )
        else:
            categories = "  ".join(
                (
                    f"{kind}:{TERRAIN_NAMES[kind]} "
                    f"{'ON' if terrain_mask & (1 << (kind - 4)) else 'OFF'}"
                )
                for kind in range(4, 10)
            )
        terrain_state = "ON" if state["terrain_master"] else "OFF"
        camera_state = "FREECAM" if state["freecam"] else "NORMAL"
        carry_state = "ON" if state["carry"] else "OFF"
        god_state = "ON" if state["god"] else "OFF"
        player_state = "ON" if state["player_debug"] else "OFF"
        counts: dict[int, int] = {}
        for shape in self.player_debug_shapes:
            kind = int(shape["kind"])
            counts[kind] = counts.get(kind, 0) + 1
        shape_summary = " ".join(
            f"{PLAYER_SHAPE_NAMES.get(kind, str(kind))}:{count}"
            for kind, count in sorted(counts.items())
        ) or "-"
        sync_hint = (
            "   F8+MOVE TO SYNC"
            if state["player_debug"] and not counts.get(3)
            else ""
        )
        return [
            categories,
            (
                f"' TERRAIN {terrain_state}   "
                f"FACES {state['terrain_count']}/{state['terrain_capacity']}"
            ),
            (
                f"V CAMERA {camera_state}   F8 CARRY {carry_state}   "
                f"G GOD {god_state}"
            ),
            (
                f"T PLAYER {player_state}   BOUNDS {shape_summary}"
                f"{sync_hint}"
            ),
        ]

    def overlay_controls_summary(self) -> str:
        path = getattr(self, "ui_panels_state", None)
        if path is None:
            return "R: OVERLAY ON"
        if time.monotonic() >= self.ui_panels_next_read:
            self.ui_panels_next_read = time.monotonic() + 0.25
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                mode = "ERROR" if state.get("error") else ("ON" if state["hidden"] else "OFF")
                self.ui_panels_summary = f"   O: HIDE GAME 2D {mode}"
            except (OSError, ValueError, KeyError):
                self.ui_panels_summary = "   O: GAME 2D UNAVAILABLE"
            try:
                visibility = json.loads(path.with_suffix(".visibility.json").read_text(encoding="utf-8"))
                if visibility.get("error"):
                    self.ui_panels_summary += "   VISIBILITY: ERROR"
                else:
                    light = "ON" if visibility.get("light") else "OFF"
                    fog = "ON" if visibility.get("no_fog") else "OFF"
                    factor = visibility.get("range_factor", 2 if visibility.get("extended") else 1)
                    distance = f"{factor}x"
                    sectors = "ALL" if visibility.get("no_pvs") else "AUTO"
                    self.ui_panels_summary += f"\nZ: LIGHT {light}   X: NO FOG {fog}   F5: RANGE {distance}\nF11: SECTORS {sectors}"
                    if not visibility.get("active"):
                        self.ui_panels_summary += " (WAIT)"
            except (OSError, ValueError):
                pass
        return "R: OVERLAY ON" + self.ui_panels_summary

    def draw_status_line(self, text: str, y: int) -> None:
        x = 12 + self.status_x_offset
        self.canvas.create_text(
            x + 1, y + 1, anchor="nw", text=text, fill="#000000",
            font=("Segoe UI", 10, "bold"),
        )
        self.canvas.create_text(
            x, y, anchor="nw", text=text, fill="#ffffff",
            font=("Segoe UI", 10, "bold"),
        )

    def player_position(self) -> tuple[float, float, float] | None:
        if self.camera_socket is not None:
            return self.last_player_position
        if not self.player_position_address:
            return None
        assert self.client is not None
        raw = self.client.read_block(
            self.player_position_address, 16, progress=False
        )
        position = struct.unpack_from("<3f", raw)
        if not all(math.isfinite(component) for component in position):
            return None
        return position

    def draw_right_line(
        self,
        width: int,
        y: int,
        text: str,
        color: str = "#ffffff",
    ) -> None:
        x = width - 18
        self.canvas.create_text(
            x + 1, y + 1, anchor="ne", text=text, fill="#000000",
            font=("Segoe UI", 10, "bold"),
        )
        self.canvas.create_text(
            x, y, anchor="ne", text=text, fill=color,
            font=("Segoe UI", 10, "bold"),
        )

    def draw_coordinate_panel(
        self,
        width: int,
        camera_position: tuple[float, float, float],
        player_position: tuple[float, float, float] | None,
    ) -> int:
        def format_position(
            label: str,
            position: tuple[float, float, float] | None,
        ) -> str:
            if position is None:
                return f"{label}: UNAVAILABLE"
            x, y, z = position
            return f"{label}: X {x:.3f}   Y {y:.3f}   Z {z:.3f}"

        y = 16
        self.draw_right_line(
            width,
            y,
            format_position("CAMERA", camera_position),
            "#48e8ff",
        )
        y += 18
        self.draw_right_line(
            width,
            y,
            format_position("PLAYER", player_position),
            "#ffae3d",
        )
        return y + 22

    def draw_active_volume_panel(
        self,
        width: int,
        position,
        y: int = 16,
    ) -> int:
        active = [
            volume
            for index, volume in enumerate(self.volumes)
            if self.volume_is_visible(index, volume)
            and point_in_volume(position, volume)
        ]
        active.sort(key=lambda volume: (volume_size(volume), volume["name"]))
        active_flags = []
        for flag in self.visible_flags():
            radius = point_in_flag_sphere(position, flag)
            if radius is not None:
                active_flags.append((radius, flag))
        active_flags.sort(key=lambda item: (item[0], item[1]["name"]))
        if not self.overlay_visible or not (active or active_flags):
            return 0

        header = "INSIDE"
        self.draw_right_line(width, y, header)
        y += 20
        for volume in active:
            label = f"[K{volume['kind']}] {volume['name']}"
            self.draw_right_line(width, y, label, color_for(volume))
            y += 18
        for radius, flag in active_flags:
            label = f"[FLAG r={radius:g}] {flag['name']}"
            self.draw_right_line(width, y, label, FLAG_TRIGGER_COLOR)
            y += 18
        return len(active) + len(active_flags)

    def draw_label(self, x: float, y: float, text: str, color: str) -> None:
        self.canvas.create_text(
            x + 1,
            y + 1,
            text=text,
            fill="#000000",
            font=("Segoe UI", 9, "bold"),
        )
        self.canvas.create_text(
            x,
            y,
            text=text,
            fill=color,
            font=("Segoe UI", 9, "bold"),
        )

    @staticmethod
    def cross(a, b):
        return (
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        )

    @staticmethod
    def normalized(vector):
        length = math.sqrt(dot(vector, vector))
        if length <= 1.0e-8:
            return (0.0, 0.0, 1.0)
        return tuple(component / length for component in vector)

    def draw_world_polyline(
        self,
        points,
        camera,
        width: int,
        height: int,
        color: str,
        *,
        closed: bool = False,
    ) -> None:
        position, right, forward, up, horizontal_slope, vertical_slope = camera
        pairs = list(zip(points, points[1:]))
        if closed and len(points) > 2:
            pairs.append((points[-1], points[0]))
        for first, second in pairs:
            segment = self.clip_near(
                self.camera_space(first, position, right, forward, up),
                self.camera_space(second, position, right, forward, up),
            )
            if segment is None:
                continue
            p1 = self.project(
                segment[0],
                width,
                height,
                horizontal_slope,
                vertical_slope,
            )
            p2 = self.project(
                segment[1],
                width,
                height,
                horizontal_slope,
                vertical_slope,
            )
            if max(abs(value) for value in (*p1, *p2)) > 20000:
                continue
            self.canvas.create_line(*p1, *p2, fill=color, width=2)

    def circle_points(self, center, first_axis, second_axis, radius):
        return [
            tuple(
                center[index]
                + first_axis[index] * math.cos(angle) * radius
                + second_axis[index] * math.sin(angle) * radius
                for index in range(3)
            )
            for angle in (
                step * (2.0 * math.pi / 24.0) for step in range(24)
            )
        ]

    def draw_flags(self, camera, width: int, height: int) -> int:
        """Draw recovered trigger spheres and optional bare AddFlag points."""
        position, right, forward, up, horizontal_slope, vertical_slope = camera
        visible_labels = 0
        all_mode = self.flag_mode == FLAG_MODE_ALL
        for flag in self.visible_flags():
            center = tuple(float(value) for value in flag["position"])
            distance = math.dist(position, center)
            if distance > (180.0 if all_mode else 450.0):
                continue
            radii = flag_sphere_radii(flag)
            color = FLAG_TRIGGER_COLOR if radii else FLAG_POINT_COLOR

            if not self.native_flag_geometry or not radii:
                # Native mode owns trigger volumes. Bare AddFlag positions
                # remain overlay crosses because they have no recovered radius.
                marker_size = 0.35
                for axis in range(3):
                    first = list(center)
                    second = list(center)
                    first[axis] -= marker_size
                    second[axis] += marker_size
                    self.draw_world_polyline(
                        (tuple(first), tuple(second)),
                        camera,
                        width,
                        height,
                        color,
                    )
                for radius in radii if not self.native_flag_geometry else ():
                    for first_axis, second_axis in (
                        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                        ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
                        ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                    ):
                        self.draw_world_polyline(
                            self.circle_points(
                                center, first_axis, second_axis, radius
                            ),
                            camera,
                            width,
                            height,
                            color,
                            closed=True,
                        )

            projected_center = self.camera_space(
                center, position, right, forward, up
            )
            if self.overlay_visible and projected_center[1] > 0.08:
                sx, sy = self.project(
                    projected_center,
                    width,
                    height,
                    horizontal_slope,
                    vertical_slope,
                )
                if -240 < sx < width + 240 and -120 < sy < height + 120:
                    suffix = (
                        " [" + ",".join(f"r={radius:g}" for radius in radii) + "]"
                        if radii
                        else ""
                    )
                    self.draw_label(
                        sx, sy, f"{flag['name']}{suffix}", color
                    )
                    visible_labels += 1
        return visible_labels

    def draw_player_debug(
        self, camera, width: int, height: int
    ) -> None:
        if (
            self.debug_state is None
            or not self.debug_state["player_debug"]
        ):
            return
        for shape in self.player_debug_shapes:
            kind = int(shape["kind"])
            first = tuple(shape["first"])
            second = tuple(shape["second"])
            radius = float(shape["radius"])
            color = PLAYER_SHAPE_COLORS.get(kind, "#ffffff")
            if kind == 2:
                axes = (
                    ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                    ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
                    ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                )
                for axis_a, axis_b in axes:
                    self.draw_world_polyline(
                        self.circle_points(
                            first, axis_a, axis_b, radius
                        ),
                        camera,
                        width,
                        height,
                        color,
                        closed=True,
                    )
            else:
                direction = tuple(
                    second[index] - first[index] for index in range(3)
                )
                axis = self.normalized(direction)
                reference = (
                    (0.0, 0.0, 1.0)
                    if abs(axis[2]) < 0.9
                    else (1.0, 0.0, 0.0)
                )
                side = self.normalized(self.cross(axis, reference))
                other = self.normalized(self.cross(axis, side))
                circles = (
                    self.circle_points(first, side, other, radius),
                    self.circle_points(second, side, other, radius),
                )
                for circle in circles:
                    self.draw_world_polyline(
                        circle,
                        camera,
                        width,
                        height,
                        color,
                        closed=True,
                    )
                for quarter in (0, 6, 12, 18):
                    self.draw_world_polyline(
                        [circles[0][quarter], circles[1][quarter]],
                        camera,
                        width,
                        height,
                        color,
                    )
        self.canvas.create_text(
            x,
            y,
            text=text,
            fill=color,
            font=("Segoe UI", 9, "bold"),
        )

    @staticmethod
    def camera_space(point, position, right, forward, up):
        relative = tuple(point[i] - position[i] for i in range(3))
        return dot(relative, right), dot(relative, forward), dot(relative, up)

    @staticmethod
    def clip_near(a, b, near=0.08):
        if a[1] < near and b[1] < near:
            return None
        a = list(a)
        b = list(b)
        if a[1] < near:
            amount = (near - a[1]) / (b[1] - a[1])
            a = [a[i] + (b[i] - a[i]) * amount for i in range(3)]
        if b[1] < near:
            amount = (near - b[1]) / (a[1] - b[1])
            b = [b[i] + (a[i] - b[i]) * amount for i in range(3)]
        return a, b

    @staticmethod
    def project(point, width, height, horizontal_slope, vertical_slope):
        focal_x = width / (2.0 * horizontal_slope)
        focal_y = height / (2.0 * vertical_slope)
        return (
            width * 0.5 + point[0] / point[1] * focal_x,
            height * 0.5 - point[2] / point[1] * focal_y,
        )

    def draw(self) -> None:
        current_window = find_game_window()
        if current_window:
            self.game_window = current_window
        if not game_window_has_focus(self.game_window) or ctypes.windll.user32.IsIconic(self.game_window):
            self.root.withdraw()
            return
        self.root.deiconify()
        current = viewport(self.game_window)
        if current is None:
            return
        if current != self.geometry:
            self.geometry = current
            x, y, width, height = current
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.canvas.configure(width=width, height=height)
        _x, _y, width, height = current
        self.canvas.delete("all")
        self.poll_toggle()
        control_state = getattr(self, 'control_state', None)
        if control_state is not None:
            try:
                state = json.loads(control_state.read_text(encoding='utf-8'))
                self.control_status = state
                toggle = state.get('hud_toggle', 0)
                if toggle != getattr(self, 'pad_hud_toggle', 0):
                    self.overlay_visible = not self.overlay_visible
                self.pad_hud_toggle = toggle
                if getattr(self, 'hud_publisher', None) is not None:
                    self.hud_publisher.publish(dict(visible=self.overlay_visible, toggle=toggle))
            except (OSError, ValueError):
                pass
        if getattr(self, 'control_status', {}).get('exit_armed'):
            self.canvas.create_text(width / 2, 12, anchor='n',
                                    text='RELEASE SELECT + START TO EXIT',
                                    fill='#ffffff', font=('Segoe UI', 10, 'bold'))
        if not self.overlay_visible:
            # Keep draining the freecam broadcast while the transparent HUD is
            # hidden, so it comes back with current camera/debug state. Native
            # in-game geometry is deliberately unaffected by this toggle.
            if self.camera_socket is not None:
                self.camera()
            return
        visible = 0
        if getattr(self, 'simple_profile', False):
            camera = self.camera()
            if camera is not None:
                self.draw_coordinate_panel(width, camera[0], self.player_position())
            self.draw_status_line('V: CAMERA   P: PAUSE   F8: CARRY   G: GOD   R: HUD   F10: EXIT', 12)
            self.draw_status_line('SELECT+L3: CONTROLLER / NORMAL   L1/R1: DOWN/UP   L2/R2: SPEED', 32)
            if self.debug_state is not None:
                state = self.debug_state
                self.draw_status_line(
                    f"CAMERA {'FREECAM' if state['freecam'] else 'NORMAL'}   "
                    f"CARRY {'ON' if state['carry'] else 'OFF'}   "
                    f"GOD {'ON' if state['god'] else 'OFF'}", 52)
            paused = getattr(self, 'control_status', {}).get('world_paused', False)
            self.draw_status_line('WORLD: PRACTICALLY PAUSED' if paused else 'WORLD: NORMAL', 72)
            return

        if self.camera_volume_panel:
            camera = self.camera()
            self.draw_status_line(self.kind_filter_summary(), 12)
            self.draw_status_line(self.flag_filter_summary(), 32)
            self.draw_status_line(self.volume_selection_summary(), 52)
            y = 72
            for line in self.debug_status_lines():
                self.draw_status_line(line, y)
                y += 20
            self.draw_status_line(self.overlay_controls_summary(), y)
            if camera is not None:
                self.draw_flags(camera, width, height)
                panel_y = self.draw_coordinate_panel(
                    width,
                    camera[0],
                    self.player_position(),
                )
                self.draw_active_volume_panel(width, camera[0], panel_y)
            return

        player_position = self.player_position()
        if player_position is not None:
            self.draw_status_line(self.kind_filter_summary(), 12)
            self.draw_status_line(self.flag_filter_summary(), 32)
            self.draw_status_line(self.volume_selection_summary(), 52)
            self.draw_status_line(self.overlay_controls_summary(), 72)
            self.draw_active_volume_panel(width, player_position)
            return

        projected = self.projected_labels()
        if projected is not None:
            if self.overlay_visible:
                for index, (volume, projection) in enumerate(zip(
                    self.volumes, projected
                )):
                    native_x, native_y, depth, depth_bias = projection
                    if not self.volume_is_visible(index, volume):
                        continue
                    if not all(
                        math.isfinite(value)
                        for value in (native_x, native_y, depth, depth_bias)
                    ):
                        continue
                    if depth <= depth_bias:
                        continue
                    sx = native_x / NATIVE_SCREEN_WIDTH * width
                    sy = native_y / NATIVE_SCREEN_HEIGHT * height
                    if -200 < sx < width + 200 and -100 < sy < height + 100:
                        self.draw_label(sx, sy, volume["name"], color_for(volume))
                        visible += 1
            legend = (
                f"R: OVERLAY {'ON' if self.overlay_visible else 'OFF'}"
                f"   exact game projection   visible: {visible}/"
                f"{self.enabled_volume_count()}/{len(self.volumes)}"
            )
            self.canvas.create_text(
                13, 13, anchor="nw", text=legend, fill="#000000",
                font=("Segoe UI", 10, "bold"),
            )
            self.canvas.create_text(
                12, 12, anchor="nw", text=legend, fill="#ffffff",
                font=("Segoe UI", 10, "bold"),
            )
            self.draw_status_line(self.kind_filter_summary(), 32)
            self.draw_status_line(self.volume_selection_summary(), 52)
            return

        camera = self.camera()
        if camera is None:
            return
        position, right, forward, up, horizontal_slope, vertical_slope = camera
        visible += self.draw_flags(camera, width, height)

        for index, volume in enumerate(self.volumes):
            if not self.volume_is_visible(index, volume):
                continue
            midpoint = volume_center(volume)
            center_relative = tuple(midpoint[i] - position[i] for i in range(3))
            if math.sqrt(dot(center_relative, center_relative)) > 450.0:
                continue
            corners = [
                self.camera_space(corner, position, right, forward, up)
                for corner in box_corners(volume, self.extent_scale)
            ]
            color = color_for(volume)
            drew = False
            for first, second in EDGES:
                segment = self.clip_near(corners[first], corners[second])
                if segment is None:
                    continue
                p1 = self.project(segment[0], width, height, horizontal_slope, vertical_slope)
                p2 = self.project(segment[1], width, height, horizontal_slope, vertical_slope)
                if max(abs(p1[0]), abs(p1[1]), abs(p2[0]), abs(p2[1])) > 20000:
                    continue
                if not self.labels_only:
                    self.canvas.create_line(*p1, *p2, fill=color, width=2)
                drew = True
            center = self.camera_space(midpoint, position, right, forward, up)
            if self.overlay_visible and drew and center[1] > 0.08:
                sx, sy = self.project(center, width, height, horizontal_slope, vertical_slope)
                if -200 < sx < width + 200 and -100 < sy < height + 100:
                    self.draw_label(sx, sy, volume["name"], color)
                    visible += 1

        legend = (
            f"R: OVERLAY {'ON' if self.overlay_visible else 'OFF'}"
            f"   {visible}/{self.enabled_volume_count()}/{len(self.volumes)} visible"
            f"   extent scale: {self.extent_scale:g}"
        )
        self.canvas.create_text(13, 13, anchor="nw", text=legend, fill="#000000", font=("Segoe UI", 10, "bold"))
        self.canvas.create_text(12, 12, anchor="nw", text=legend, fill="#ffffff", font=("Segoe UI", 10, "bold"))
        self.draw_status_line(self.kind_filter_summary(), 32)
        self.draw_status_line(self.flag_filter_summary(), 52)
        self.draw_status_line(self.volume_selection_summary(), 72)

    def tick(self) -> None:
        if time.monotonic() >= self.deadline:
            self.close()
            return
        try:
            self.draw()
        except (ConnectionError, OSError, RuntimeError):
            self.close()
            raise
        self.root.after(16, self.tick)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.camera_socket is not None:
            self.camera_socket.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.after(0, self.tick)
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("volumes", type=Path)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--fov", type=float, default=65.0)
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--freecam", action="store_true")
    parser.add_argument("--freecam-speed", type=float, default=8.0)
    parser.add_argument(
        "--camera-state-port",
        type=int,
        default=0,
        help="listen for freecam UDP state without opening a second PINE client",
    )
    parser.add_argument(
        "--extent-scale",
        type=float,
        default=0.5,
        help="Multiplier from Lua full size to corner extent (default: 0.5)",
    )
    parser.add_argument(
        "--labels-only",
        action="store_true",
        help="draw names only; leave the depth-tested boxes to the in-game renderer",
    )
    parser.add_argument(
        "--projected-label-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE address printed as ACTIVE_LABEL_POINTER by all_volumes_probe",
    )
    parser.add_argument(
        "--player-position-address",
        type=lambda value: int(value, 0),
        default=0,
        help="EE address printed as ACTIVE_PLAYER_POSITION_POINTER",
    )
    parser.add_argument(
        "--camera-volume-panel",
        action="store_true",
        help="list boxes containing the freecam position received over UDP",
    )
    parser.add_argument(
        "--native-flag-geometry",
        action="store_true",
        help="draw only flag labels; native renderer owns their 3D geometry",
    )
    parser.add_argument(
        "--volume-order",
        default="",
        help="comma-separated source indices matching native renderer order",
    )
    parser.add_argument(
        "--status-x-offset",
        type=int,
        default=0,
        help="move the upper-left status text right without moving 3D labels",
    )
    parser.add_argument("--ui-panels-state", type=Path)
    parser.add_argument('--control-state', type=Path)
    parser.add_argument('--profile', choices=('freecam', 'maptriggers'), default='maptriggers')
    parser.add_argument('--ready-file', type=Path)
    args = parser.parse_args()
    document = json.loads(args.volumes.read_text(encoding="utf-8"))
    volumes = list(document["volumes"])
    flags = list(document.get("flags", []))
    if args.volume_order:
        volume_order = [
            int(value) for value in args.volume_order.split(",")
        ]
        if sorted(volume_order) != list(range(len(volumes))):
            raise ValueError(
                "volume-order must be a permutation of every source index"
            )
        volumes = [volumes[index] for index in volume_order]
    if args.projected_label_address:
        if args.volume_order:
            raise ValueError(
                "volume-order and projected-label ordering are mutually exclusive"
            )
        volumes = order_by_current_camera(volumes, args.port)
    camera_state_port = args.camera_state_port
    if args.freecam and not camera_state_port:
        camera_state_port = 28012
    if args.camera_volume_panel and not camera_state_port:
        raise ValueError("camera-volume-panel requires --camera-state-port")
    overlay = Overlay(
        volumes,
        flags,
        args.duration,
        args.fov,
        args.port,
        camera_state_port,
        args.extent_scale,
        args.labels_only,
        args.projected_label_address,
        args.player_position_address,
        args.camera_volume_panel,
        args.native_flag_geometry,
        args.status_x_offset,
        args.ui_panels_state,
    )
    overlay.control_state = args.control_state
    overlay.simple_profile = args.profile == 'freecam'
    if args.control_state is not None:
        from overlay_status import StatusPublisher, read_status
        hud_path = args.control_state.with_name('hud.json')
        saved_hud = read_status(hud_path, pending=True)
        overlay.overlay_visible = bool(saved_hud.get('visible', True))
        overlay.pad_hud_toggle = int(saved_hud.get('toggle', 0))
        overlay.hud_publisher = StatusPublisher(hud_path)
    if args.ready_file is not None:
        args.ready_file.write_text('ready', encoding='ascii')
    freecam_process = None
    if args.freecam:
        freecam_process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("freecam_probe.py")),
                "--duration",
                str(args.duration),
                "--speed",
                str(args.freecam_speed),
                "--port",
                str(args.port),
                "--broadcast-port",
                str(camera_state_port),
            ]
        )
    try:
        overlay.run()
    finally:
        if freecam_process is not None:
            try:
                freecam_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                freecam_process.terminate()
                freecam_process.wait(timeout=2.0)


if __name__ == "__main__":
    main()
