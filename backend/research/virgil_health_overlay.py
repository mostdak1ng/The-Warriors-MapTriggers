#!/usr/bin/env python3
"""Read-only, savestate-safe Virgil health HUD for The Warriors US v1.03."""

from __future__ import annotations

import argparse
import ctypes
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace

from pine_client import GAME_ID, GAME_VERSION, UUID, PineClient
from player_noclip import ENTITY_REGISTRY, plausible_ee_pointer


SUPPORTED_GAME = ("SLUS-21215", "1.03", "b99a75de")
HUMAN_VTABLE = 0x0053F088
REGISTRY_ENTRIES = 0x4000
HUMAN_NAME_OFFSET = 0x80
HUMAN_CHAR_TYPE_OFFSET = 0xCC
HUMAN_STATE_OFFSET = 0xD4
STATE_HEALTH_OFFSET = 0x144
STATE_MAX_HEALTH_OFFSET = 0x146
VIRGIL_CHAR_TYPE = 178
PHASE_1_HP = 4150
PHASE_2_HP = 3650

COLOR_KEY = "#010203"
PANEL_BG = "#0d151e"
PANEL_BORDER = "#4ac9ed"
TEXT = "#edf6fa"
DIM = "#91a3ad"
CYAN = "#65dcff"
GREEN = "#71f59b"
YELLOW = "#ffd166"
RED = "#ff7474"

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_Q = 0x51
SINGLE_INSTANCE_MUTEX = "Local\\TheWarriors_VirgilHealthOverlay"


class Point(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))


class Rect(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


@dataclass(frozen=True)
class HealthSnapshot:
    message: str = "Connecting to PCSX2..."
    hp: int | None = None
    max_hp: int | None = None
    human: int = 0
    state: int = 0
    handle: int = 0
    last_delta: int | None = None
    last_change_time: float = 0.0
    reconnects: int = 0


def pressed(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def acquire_single_instance() -> int:
    """Hold a Windows mutex so a second HUD cannot monopolize PINE."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    )
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = int(kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX) or 0)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return 0
    return handle


def release_single_instance(handle: int) -> None:
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def aligned_range(address: int, size: int) -> tuple[int, int]:
    """Return the smallest 8-byte-aligned PINE range covering a field."""
    base = address & ~7
    end = (address + size + 7) & ~7
    return base, end - base


def unpack_u32(raw: bytes, range_address: int, field_address: int) -> int:
    return struct.unpack_from("<I", raw, field_address - range_address)[0]


def find_game_window() -> int:
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    exact = int(user32.FindWindowW(None, "The Warriors") or 0)
    if exact:
        return exact

    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    @callback_type
    def callback(hwnd: int, _parameter: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if "The Warriors" in buffer.value:
            matches.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback, 0)
    return matches[0] if matches else 0


def viewport(hwnd: int) -> tuple[int, int, int, int] | None:
    user32 = ctypes.windll.user32
    if not hwnd or user32.IsIconic(hwnd):
        return None
    rect = Rect()
    origin = Point(0, 0)
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 1 or height <= 1:
        return None
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    fullscreen = width >= screen_w - 4 and height >= screen_h - 4
    top_inset = 0 if fullscreen else 21
    bottom_inset = 0 if fullscreen else 25
    return origin.x, origin.y + top_inset, width, height - top_inset - bottom_inset


def game_is_foreground(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    foreground = int(user32.GetForegroundWindow() or 0)
    if not foreground:
        return False
    user32.GetAncestor.restype = ctypes.c_void_p
    root = int(user32.GetAncestor(foreground, 2) or foreground)
    return root == hwnd


class VirgilHealthWorker(threading.Thread):
    """Continuously resolves Virgil instead of trusting pre-load pointers."""

    def __init__(self, *, port: int, sample_hz: float) -> None:
        super().__init__(name="VirgilHealthWorker", daemon=True)
        self.port = port
        self.sample_hz = sample_hz
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = HealthSnapshot()
        self.human = 0
        self.state = 0
        self.handle = 0
        self.previous_hp: int | None = None

    def snapshot(self) -> HealthSnapshot:
        with self.lock:
            return self.latest

    def publish(self, **changes: object) -> None:
        with self.lock:
            self.latest = replace(self.latest, **changes)

    @staticmethod
    def verify_game(client: PineClient) -> None:
        identity = (
            client.text(GAME_ID),
            client.text(GAME_VERSION),
            client.text(UUID).lower(),
        )
        if identity != SUPPORTED_GAME:
            raise RuntimeError(
                "Unsupported game: "
                f"serial={identity[0]} version={identity[1]} crc={identity[2]}"
            )

    def clear_target(self) -> None:
        self.human = 0
        self.state = 0
        self.handle = 0
        self.previous_hp = None

    def resolve_virgil(self, client: PineClient) -> bool:
        registry = client.read_block(
            ENTITY_REGISTRY, REGISTRY_ENTRIES * 8, progress=False
        )
        candidates: list[int] = []
        handles: dict[int, int] = {}
        seen: set[int] = set()
        for index in range(REGISTRY_ENTRIES):
            human, generation = struct.unpack_from("<IH", registry, index * 8)
            if human in seen or not plausible_ee_pointer(human):
                continue
            seen.add(human)
            candidates.append(human)
            handles[human] = (index << 16) | generation

        if not candidates:
            return False

        vtable_ranges = [aligned_range(human, 4) for human in candidates]
        vtable_rows = client.read_blocks(vtable_ranges)
        humans = [
            human
            for human, raw, memory_range in zip(
                candidates, vtable_rows, vtable_ranges
            )
            if unpack_u32(raw, memory_range[0], human) == HUMAN_VTABLE
        ]
        if not humans:
            return False

        detail_ranges = [
            memory_range
            for human in humans
            for memory_range in (
                aligned_range(human + HUMAN_NAME_OFFSET, 32),
                aligned_range(human + 0xC8, 16),
            )
        ]
        details = client.read_blocks(detail_ranges)
        for index, human in enumerate(humans):
            name_raw = details[index * 2]
            metadata = details[index * 2 + 1]
            name_base = detail_ranges[index * 2][0]
            metadata_base = detail_ranges[index * 2 + 1][0]
            name_offset = human + HUMAN_NAME_OFFSET - name_base
            name = name_raw[name_offset : name_offset + 32]
            name = name.split(b"\0", 1)[0].decode("ascii", errors="replace")
            char_type = unpack_u32(
                metadata,
                metadata_base,
                human + HUMAN_CHAR_TYPE_OFFSET,
            )
            state = unpack_u32(
                metadata,
                metadata_base,
                human + HUMAN_STATE_OFFSET,
            )
            if (
                name == "Virgil"
                and char_type == VIRGIL_CHAR_TYPE
                and plausible_ee_pointer(state)
            ):
                self.human = human
                self.state = state
                self.handle = handles[human]
                self.previous_hp = None
                return True
        return False

    def read_health(self, client: PineClient) -> tuple[int, int]:
        if not self.human or not self.state:
            raise RuntimeError("Virgil is unresolved")

        ranges = [
            aligned_range(self.human, 4),
            aligned_range(self.human + HUMAN_NAME_OFFSET, 32),
            aligned_range(self.human + 0xC8, 16),
        ]
        vtable_raw, name_raw, metadata = client.read_blocks(ranges)
        vtable = unpack_u32(vtable_raw, ranges[0][0], self.human)
        name_offset = self.human + HUMAN_NAME_OFFSET - ranges[1][0]
        name = name_raw[name_offset : name_offset + 32]
        name = name.split(b"\0", 1)[0].decode("ascii", errors="replace")
        char_type = unpack_u32(
            metadata,
            ranges[2][0],
            self.human + HUMAN_CHAR_TYPE_OFFSET,
        )
        state = unpack_u32(
            metadata,
            ranges[2][0],
            self.human + HUMAN_STATE_OFFSET,
        )
        if (
            vtable != HUMAN_VTABLE
            or name != "Virgil"
            or char_type != VIRGIL_CHAR_TYPE
            or not plausible_ee_pointer(state)
        ):
            raise RuntimeError("Virgil entity changed while loading the state")

        self.state = state
        health_range = aligned_range(state + STATE_HEALTH_OFFSET, 4)
        health_raw = client.read_block(*health_range, progress=False)
        hp, max_hp = struct.unpack_from(
            "<HH",
            health_raw,
            state + STATE_HEALTH_OFFSET - health_range[0],
        )
        if max_hp == 0:
            raise RuntimeError("Maximum health is temporarily invalid")
        return hp, max_hp

    def run_connection(self, client: PineClient) -> None:
        self.verify_game(client)
        self.clear_target()
        next_scan = 0.0
        interval = 1.0 / self.sample_hz
        while not self.stop_event.is_set():
            start = time.perf_counter()
            if not self.human:
                if start < next_scan:
                    self.stop_event.wait(min(next_scan - start, 0.05))
                    continue
                if not self.resolve_virgil(client):
                    self.publish(
                        message="Esperando a Virgil...",
                        hp=None,
                        max_hp=None,
                        human=0,
                        state=0,
                        handle=0,
                        last_delta=None,
                    )
                    next_scan = start + 0.35
                    continue

            try:
                hp, max_hp = self.read_health(client)
            except (RuntimeError, ValueError, struct.error):
                self.clear_target()
                next_scan = 0.0
                continue

            delta: int | None = None
            changed_at = self.latest.last_change_time
            if self.previous_hp is not None and hp != self.previous_hp:
                delta = hp - self.previous_hp
                changed_at = time.monotonic()
            elif self.previous_hp is not None:
                delta = self.latest.last_delta
            self.previous_hp = hp
            self.publish(
                message="ONLINE · read only · resolves again after each load",
                hp=hp,
                max_hp=max_hp,
                human=self.human,
                state=self.state,
                handle=self.handle,
                last_delta=delta,
                last_change_time=changed_at,
            )
            elapsed = time.perf_counter() - start
            self.stop_event.wait(max(0.0, interval - elapsed))

    def run(self) -> None:
        while not self.stop_event.is_set():
            client: PineClient | None = None
            try:
                self.publish(
                    message="Connecting to PCSX2...",
                    hp=None,
                    max_hp=None,
                    human=0,
                    state=0,
                    handle=0,
                    last_delta=None,
                )
                client = PineClient(port=self.port, timeout=0.75)
                self.run_connection(client)
            except Exception as error:
                self.clear_target()
                self.publish(
                    message=f"Waiting for game: {error}",
                    hp=None,
                    max_hp=None,
                    human=0,
                    state=0,
                    handle=0,
                    last_delta=None,
                    reconnects=self.latest.reconnects + 1,
                )
                self.stop_event.wait(0.5)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass

    def stop(self) -> None:
        self.stop_event.set()


class VirgilOverlay:
    def __init__(
        self,
        root: tk.Tk,
        *,
        worker: VirgilHealthWorker,
        x_offset: int,
        y_offset: int,
    ) -> None:
        self.root = root
        self.worker = worker
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.game_window = 0
        self.geometry: tuple[int, int, int, int] | None = None
        self.quit_down = False
        self.closed = False

        root.title("Virgil Exact Health")
        root.overrideredirect(True)
        root.configure(bg=COLOR_KEY)
        root.attributes("-topmost", True)
        root.wm_attributes("-transparentcolor", COLOR_KEY)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(
            root,
            bg=COLOR_KEY,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        root.update_idletasks()
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

    def line(
        self,
        x: int,
        y: int,
        text: str,
        *,
        color: str = TEXT,
        font: tuple[str, int, str] = ("Consolas", 11, "normal"),
        anchor: str = "nw",
    ) -> None:
        self.canvas.create_text(
            x, y, anchor=anchor, text=text, fill=color, font=font
        )

    @staticmethod
    def phase_text(hp: int) -> tuple[str, str]:
        if hp > PHASE_1_HP:
            return f"{hp - PHASE_1_HP} HP sobre el corte de fase 1", GREEN
        if hp == PHASE_1_HP:
            return "EXACTO en corte de fase 1 (4150)", YELLOW
        if hp > PHASE_2_HP:
            return "Entre los cortes 4150 y 3650", CYAN
        if hp == PHASE_2_HP:
            return "EXACTO en corte de fase 2 (3650)", YELLOW
        return f"{PHASE_2_HP - hp} HP debajo del corte de fase 2", RED

    def draw_panel(self, snapshot: HealthSnapshot) -> None:
        left = self.x_offset
        top = self.y_offset
        width = 460
        height = 142
        self.canvas.create_rectangle(
            left,
            top,
            left + width,
            top + height,
            fill=PANEL_BG,
            outline=PANEL_BORDER,
            width=2,
        )
        self.line(
            left + 13,
            top + 9,
            "VIRGIL · HP EXACTO",
            color=CYAN,
            font=("Consolas", 12, "bold"),
        )
        self.line(
            left + width - 13,
            top + 11,
            "Ctrl+Shift+Q exit",
            color=DIM,
            font=("Consolas", 8, "normal"),
            anchor="ne",
        )

        if snapshot.hp is None or snapshot.max_hp is None:
            self.line(
                left + 13,
                top + 42,
                "-- / --",
                color=YELLOW,
                font=("Consolas", 24, "bold"),
            )
            self.line(
                left + 13,
                top + 91,
                snapshot.message[:62],
                color=DIM,
                font=("Consolas", 9, "normal"),
            )
            return

        hp = snapshot.hp
        max_hp = snapshot.max_hp
        percent = hp * 100.0 / max_hp
        hp_color = GREEN if hp > PHASE_1_HP else CYAN
        if hp <= PHASE_2_HP:
            hp_color = RED
        self.line(
            left + 13,
            top + 37,
            f"{hp:4d} / {max_hp:4d}",
            color=hp_color,
            font=("Consolas", 25, "bold"),
        )
        self.line(
            left + 264,
            top + 45,
            f"{percent:6.2f}%",
            color=TEXT,
            font=("Consolas", 18, "bold"),
        )
        if (
            snapshot.last_delta is not None
            and time.monotonic() - snapshot.last_change_time <= 4.0
        ):
            delta_color = RED if snapshot.last_delta < 0 else GREEN
            self.line(
                left + width - 13,
                top + 51,
                f"cambio {snapshot.last_delta:+d}",
                color=delta_color,
                font=("Consolas", 10, "bold"),
                anchor="ne",
            )

        bar_left = left + 14
        bar_top = top + 82
        bar_width = width - 28
        bar_height = 10
        self.canvas.create_rectangle(
            bar_left,
            bar_top,
            bar_left + bar_width,
            bar_top + bar_height,
            fill="#23313b",
            outline="#3f5968",
        )
        fill_width = round(bar_width * max(0.0, min(1.0, hp / max_hp)))
        if fill_width:
            self.canvas.create_rectangle(
                bar_left,
                bar_top,
                bar_left + fill_width,
                bar_top + bar_height,
                fill=hp_color,
                outline="",
            )
        for threshold in (PHASE_2_HP, PHASE_1_HP):
            marker_x = bar_left + round(bar_width * threshold / max_hp)
            self.canvas.create_line(
                marker_x,
                bar_top - 3,
                marker_x,
                bar_top + bar_height + 3,
                fill=YELLOW,
                width=2,
            )

        phase, phase_color = self.phase_text(hp)
        self.line(
            left + 13,
            top + 101,
            phase,
            color=phase_color,
            font=("Consolas", 10, "bold"),
        )
        self.line(
            left + width - 13,
            top + 103,
            "P1 4150 · P2 3650",
            color=DIM,
            font=("Consolas", 9, "normal"),
            anchor="ne",
        )
        self.line(
            left + 13,
            top + 122,
            f"human=0x{snapshot.human:08X}  state=0x{snapshot.state:08X}",
            color=DIM,
            font=("Consolas", 8, "normal"),
        )

    def tick(self) -> None:
        if self.closed:
            return
        quit_now = pressed(VK_CONTROL) and pressed(VK_SHIFT) and pressed(VK_Q)
        if quit_now and not self.quit_down:
            self.close()
            return
        self.quit_down = quit_now

        current_window = find_game_window()
        if current_window:
            self.game_window = current_window
        current = viewport(self.game_window)
        if current is None or not game_is_foreground(self.game_window):
            if self.geometry is not None:
                self.geometry = None
                self.root.geometry("1x1+-32000+-32000")
            self.root.after(33, self.tick)
            return
        if current != self.geometry:
            self.geometry = current
            screen_x, screen_y, width, height = current
            self.root.geometry(f"{width}x{height}+{screen_x}+{screen_y}")
            self.canvas.configure(width=width, height=height)
        self.canvas.delete("all")
        self.draw_panel(self.worker.snapshot())
        self.root.after(33, self.tick)

    def run(self) -> None:
        self.root.after(0, self.tick)
        self.root.mainloop()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.worker.stop()
        self.worker.join(timeout=1.5)
        self.root.destroy()


def probe_once(port: int) -> int:
    client = PineClient(port=port, timeout=0.75)
    worker = VirgilHealthWorker(port=port, sample_hz=20.0)
    try:
        worker.verify_game(client)
        if not worker.resolve_virgil(client):
            print("Virgil is not loaded")
            return 2
        hp, max_hp = worker.read_health(client)
        print(
            f"Virgil HP={hp}/{max_hp} ({hp * 100.0 / max_hp:.2f}%) "
            f"human=0x{worker.human:08X} state=0x{worker.state:08X} "
            f"handle=0x{worker.handle:08X}"
        )
        return 0
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only external Virgil health overlay"
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--sample-hz", type=float, default=30.0)
    parser.add_argument("--x-offset", type=int, default=18)
    parser.add_argument("--y-offset", type=int, default=18)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 2.0 <= args.sample_hz <= 120.0:
        raise ValueError("--sample-hz must be between 2 and 120")
    if args.once:
        probe_handle = acquire_single_instance()
        if not probe_handle:
            print("The overlay is already running and using PINE")
            raise SystemExit(3)
        try:
            raise SystemExit(probe_once(args.port))
        finally:
            release_single_instance(probe_handle)

    instance_handle = acquire_single_instance()
    if not instance_handle:
        return
    try:
        worker = VirgilHealthWorker(port=args.port, sample_hz=args.sample_hz)
        worker.start()
        root = tk.Tk()
        overlay = VirgilOverlay(
            root,
            worker=worker,
            x_offset=args.x_offset,
            y_offset=args.y_offset,
        )
        try:
            overlay.run()
        finally:
            if not overlay.closed:
                overlay.close()
    finally:
        release_single_instance(instance_handle)


if __name__ == "__main__":
    main()
