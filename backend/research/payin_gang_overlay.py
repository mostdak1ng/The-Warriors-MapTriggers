#!/usr/bin/env python3
"""Read-only per-enemy gang HUD for Payin' The Cost checkpoints 2 and 3.

The overlay follows every relevant Destroyer on screen and reads the actor's
native gang pointer.  It therefore keeps Plumbing A/B and Voodoo A/B separate
after the actors mix.  No game memory is written.

Supported game: The Warriors US v1.03 (SLUS-21215, CRC b99a75de).
"""

from __future__ import annotations

import argparse
import ctypes
import math
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace

from freecam_probe import resolve_camera_transform
from pine_client import GAME_ID, GAME_VERSION, UUID, PineClient
from player_noclip import (
    ENTITY_REGISTRY,
    HUMAN_MOVE_INDEX_OFFSET,
    MOVE_TRANSFORM_BASE,
    MOVE_TRANSFORM_STRIDE,
    plausible_ee_pointer,
)
from virgil_health_overlay import (
    find_game_window,
    game_is_foreground,
    pressed,
    viewport,
)


SUPPORTED_GAME = ("SLUS-21215", "1.03", "b99a75de")
HUMAN_VTABLE = 0x0053F088
REGISTRY_ENTRIES = 0x4000
HUMAN_SCAN_SIZE = 0xE8
HUMAN_NAME_OFFSET = 0x80
HUMAN_STATE_OFFSET = 0xD4
STATE_HEALTH_OFFSET = 0x144

# Brain slots are indexed by Human + 0x92.  +0x20C points at the live native
# gang record.  The gang record contains its fixed 16-byte name and the same
# 32-member handle array used by the native headcount functions.
BRAIN_BASE = 0x006D53F0
BRAIN_STRIDE = 0x2F0
BRAIN_GANG_POINTER_OFFSET = 0x20C
GANG_NAME_OFFSET = 0x1C
GANG_NAME_SIZE = 16
GANG_MEMBER_ARRAY_OFFSET = 0x48
GANG_MEMBER_CAPACITY = 32
GANG_READ_SIZE = GANG_MEMBER_ARRAY_OFFSET + GANG_MEMBER_CAPACITY * 4
NIL_HANDLE = 0xFFFFFFFF

COLOR_KEY = "#010203"
LABEL_BG = "#0a1118"
PANEL_BG = "#0d151e"
PANEL_BORDER = "#4ac9ed"
TEXT = "#f3f8fb"
DIM = "#91a3ad"
CYAN = "#65dcff"
GREEN = "#71f59b"
YELLOW = "#ffd166"
ORANGE = "#ffad66"
RED = "#ff7474"
MAGENTA = "#ff8ee5"
RETIRING_GRAY = "#a3a9ae"
PLUMBING_A = "#55ddff"
PLUMBING_B = "#7890ff"
VOODOO_A = "#ff68d5"
VOODOO_B = "#b7ff5c"

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_Q = 0x51
SINGLE_INSTANCE_MUTEX = "Local\\TheWarriors_PayinGangOverlay"


@dataclass(frozen=True)
class GangSpec:
    label: str
    color: str
    counts_for_c3: bool
    category: str


GANG_SPECS = {
    "DestroyerWave07": GangSpec("BUDDHA C3", YELLOW, True, "c3"),
    "DestroyerWave04": GangSpec("PLUMBING A", PLUMBING_A, True, "c3"),
    "DestroyerWave04b": GangSpec("PLUMBING B", PLUMBING_B, True, "c3"),
    "DestroyerWave05": GangSpec("VOODOO A", VOODOO_A, True, "c3"),
    "DestroyerWave05b": GangSpec("VOODOO B", VOODOO_B, True, "c3"),
    "DestroyerWave06": GangSpec("MEAT", ORANGE, True, "c3"),
    "DestroyerWave01": GangSpec("VOODOO C2", DIM, False, "c2"),
    "DestroyerWave02": GangSpec("BUDDHA C2", DIM, False, "c2"),
    "DestroyerWave03": GangSpec("MEAT C2", DIM, False, "c2"),
    "DestroyerSpawner": GangSpec("SPAWNER EXTRA", RED, False, "spawner"),
    # A ghost belongs here only for the very small interval between HuCreate
    # and GangAddMember.  Normally it is already visible under its inherited
    # counted gang.
    "GangDestroyerGhosts": GangSpec(
        "RESSURECT TRANSITORIO", RED, False, "ghost_pool"
    ),
}


@dataclass(frozen=True)
class HumanMeta:
    human: int
    handle: int
    name: str
    state: int
    move_index: int


@dataclass(frozen=True)
class EnemyReading:
    human: int
    handle: int
    actor_name: str
    gang_name: str
    gang_label: str
    gang_color: str
    remaining: int
    origin: str
    counts_for_c3: bool
    hp: int
    max_hp: int
    position: tuple[float, float, float]


@dataclass(frozen=True)
class GangReading:
    name: str
    label: str
    color: str
    remaining: int
    counts_for_c3: bool


@dataclass(frozen=True)
class PanelGangReading:
    label: str
    color: str
    remaining: int
    retiring: bool


PANEL_GROUPS = (
    ("BUDDHA", YELLOW, ("DestroyerWave07",)),
    ("PLUMBING", CYAN, ("DestroyerWave04", "DestroyerWave04b")),
    ("VOODOO", MAGENTA, ("DestroyerWave05", "DestroyerWave05b")),
    ("MEAT", ORANGE, ("DestroyerWave06",)),
)


@dataclass(frozen=True)
class WorldSnapshot:
    message: str = "Connecting to PCSX2..."
    enemies: tuple[EnemyReading, ...] = ()
    gangs: tuple[GangReading, ...] = ()
    camera: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
        float,
        float,
    ] | None = None
    reconnects: int = 0


def acquire_single_instance() -> int:
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
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return 0
    return handle


def release_single_instance(handle: int) -> None:
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def aligned_range(address: int, size: int) -> tuple[int, int]:
    base = address & ~7
    end = (address + size + 7) & ~7
    return base, end - base


def unpack_u32(raw: bytes, base: int, address: int) -> int:
    return struct.unpack_from("<I", raw, address - base)[0]


def decode_fixed_name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", errors="replace")


def decode_gang_name(raw: bytes) -> str:
    if len(raw) < GANG_NAME_OFFSET + GANG_NAME_SIZE:
        raise ValueError("registro de gang truncado")
    return decode_fixed_name(
        raw[GANG_NAME_OFFSET : GANG_NAME_OFFSET + GANG_NAME_SIZE]
    )


def plausible_gang_pointer(pointer: int) -> bool:
    """Gang records live just below the normal heap's pointer floor."""
    return 0x00500000 <= pointer < 0x02000000 and pointer % 8 == 0


def gang_headcount(raw: bytes) -> int:
    if len(raw) < GANG_READ_SIZE:
        raise ValueError("truncated gang member list")
    handles = struct.unpack_from(
        f"<{GANG_MEMBER_CAPACITY}I", raw, GANG_MEMBER_ARRAY_OFFSET
    )
    return sum(handle != NIL_HANDLE for handle in handles)


def classify_origin(actor_name: str, spec: GangSpec) -> str:
    if actor_name.startswith("GangDestroyersG"):
        return "RESSURECT"
    if spec.category == "c3":
        return "ORIGINAL"
    if spec.category == "c2":
        return "INHERITED C2"
    if spec.category == "spawner":
        return "SPAWNER · DOES NOT COUNT"
    return "RESSURECT"


def display_gang_color(spec: GangSpec, remaining: int) -> str:
    if spec.counts_for_c3 and remaining <= 2:
        return RETIRING_GRAY
    return spec.color


def aggregate_panel_gangs(
    gangs: tuple[GangReading, ...],
) -> tuple[PanelGangReading, ...]:
    by_name = {gang.name: gang for gang in gangs if gang.counts_for_c3}
    result: list[PanelGangReading] = []
    for label, color, member_names in PANEL_GROUPS:
        present = [by_name[name] for name in member_names if name in by_name]
        if not present:
            continue
        retiring = len(present) == len(member_names) and all(
            gang.remaining <= 2 for gang in present
        )
        result.append(
            PanelGangReading(
                label=label,
                color=RETIRING_GRAY if retiring else color,
                remaining=sum(gang.remaining for gang in present),
                retiring=retiring,
            )
        )
    return tuple(result)


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


def transform_address(meta: HumanMeta) -> int:
    if 0 <= meta.move_index < 0x4000:
        return MOVE_TRANSFORM_BASE + meta.move_index * MOVE_TRANSFORM_STRIDE
    return meta.human + 0x2C0


def decode_position(raw: bytes) -> tuple[float, float, float]:
    position = struct.unpack_from("<3f", raw)
    if not all(math.isfinite(value) for value in position):
        raise ValueError("non-finite position")
    return position


def rotate_quaternion(
    quaternion: tuple[float, ...],
    vector: tuple[float, ...],
) -> tuple[float, float, float]:
    x, y, z, w = quaternion
    vx, vy, vz = vector
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def decode_camera(raw: bytes) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    float,
    float,
]:
    values = struct.unpack_from("<8f", raw)
    position = tuple(values[:3])
    quaternion = tuple(values[4:8])
    if not all(math.isfinite(value) for value in (*position, *quaternion)):
        raise ValueError("camera contains non-finite values")
    right = rotate_quaternion(quaternion, (1.0, 0.0, 0.0))
    forward = rotate_quaternion(quaternion, (0.0, 1.0, 0.0))
    up = rotate_quaternion(quaternion, (0.0, 0.0, 1.0))
    horizontal_slope, vertical_slope = struct.unpack_from("<2f", raw, 0x4C)
    if not (
        math.isfinite(horizontal_slope)
        and math.isfinite(vertical_slope)
        and 0.01 < horizontal_slope < 20.0
        and 0.01 < vertical_slope < 20.0
    ):
        raise ValueError("invalid camera frustum")
    return (
        position,
        right,
        forward,
        up,
        horizontal_slope,
        vertical_slope,
    )


class WorldReader:
    """Resolve actors, native gangs and camera without retaining pointers."""

    @staticmethod
    def read_humans(client: PineClient) -> list[HumanMeta]:
        registry = client.read_block(
            ENTITY_REGISTRY,
            REGISTRY_ENTRIES * 8,
            progress=False,
        )
        pointer_handles: dict[int, int] = {}
        for index in range(REGISTRY_ENTRIES):
            human, generation = struct.unpack_from("<IH", registry, index * 8)
            if plausible_ee_pointer(human):
                pointer_handles.setdefault(human, (index << 16) | generation)

        pointers = list(pointer_handles)
        header_ranges = [(pointer & ~7, 16) for pointer in pointers]
        headers = client.read_blocks(header_ranges)
        human_pointers = [
            pointer
            for pointer, raw in zip(pointers, headers, strict=True)
            if struct.unpack_from("<I", raw, pointer & 7)[0] == HUMAN_VTABLE
        ]
        detail_ranges = [
            (
                pointer & ~7,
                (HUMAN_SCAN_SIZE + (pointer & 7) + 7) & ~7,
            )
            for pointer in human_pointers
        ]
        details = client.read_blocks(detail_ranges)
        result: list[HumanMeta] = []
        for pointer, raw in zip(human_pointers, details, strict=True):
            prefix = pointer & 7
            block = raw[prefix : prefix + HUMAN_SCAN_SIZE]
            result.append(
                HumanMeta(
                    human=pointer,
                    handle=pointer_handles[pointer],
                    name=decode_fixed_name(
                        block[HUMAN_NAME_OFFSET : HUMAN_NAME_OFFSET + 16]
                    ),
                    state=struct.unpack_from("<I", block, HUMAN_STATE_OFFSET)[0],
                    move_index=struct.unpack_from(
                        "<h", block, HUMAN_MOVE_INDEX_OFFSET
                    )[0],
                )
            )
        return result

    @staticmethod
    def read_gang_pointers(
        client: PineClient,
        humans: list[HumanMeta],
    ) -> dict[int, int]:
        eligible = [human for human in humans if 0 <= human.move_index < 0x100]
        ranges = [
            aligned_range(
                BRAIN_BASE
                + human.move_index * BRAIN_STRIDE
                + BRAIN_GANG_POINTER_OFFSET,
                4,
            )
            for human in eligible
        ]
        rows = client.read_blocks(ranges)
        result: dict[int, int] = {}
        for human, raw, memory_range in zip(
            eligible, rows, ranges, strict=True
        ):
            address = (
                BRAIN_BASE
                + human.move_index * BRAIN_STRIDE
                + BRAIN_GANG_POINTER_OFFSET
            )
            pointer = unpack_u32(raw, memory_range[0], address)
            if plausible_gang_pointer(pointer):
                result[human.human] = pointer
        return result

    def read(self, client: PineClient) -> WorldSnapshot:
        humans = self.read_humans(client)
        gang_pointers = self.read_gang_pointers(client, humans)
        unique_gangs = sorted(set(gang_pointers.values()))
        gang_ranges = [(pointer, GANG_READ_SIZE) for pointer in unique_gangs]
        gang_rows = client.read_blocks(gang_ranges)
        gang_raw = dict(zip(unique_gangs, gang_rows, strict=True))

        relevant: list[tuple[HumanMeta, int, str, GangSpec, int]] = []
        gangs_by_name: dict[str, GangReading] = {}
        for human in humans:
            gang_pointer = gang_pointers.get(human.human)
            if gang_pointer is None:
                continue
            raw = gang_raw.get(gang_pointer)
            if raw is None:
                continue
            gang_name = decode_gang_name(raw)
            spec = GANG_SPECS.get(gang_name)
            if spec is None:
                continue
            remaining = gang_headcount(raw)
            color = display_gang_color(spec, remaining)
            relevant.append((human, gang_pointer, gang_name, spec, remaining))
            gangs_by_name[gang_name] = GangReading(
                name=gang_name,
                label=spec.label,
                color=color,
                remaining=remaining,
                counts_for_c3=spec.counts_for_c3,
            )

        states = [
            (human, aligned_range(human.state + STATE_HEALTH_OFFSET, 4))
            for human, _pointer, _name, _spec, _remaining in relevant
            if plausible_ee_pointer(human.state)
        ]
        state_rows = client.read_blocks([memory_range for _human, memory_range in states])
        health: dict[int, tuple[int, int]] = {}
        for (human, memory_range), raw in zip(states, state_rows, strict=True):
            hp, max_hp = struct.unpack_from(
                "<HH",
                raw,
                human.state + STATE_HEALTH_OFFSET - memory_range[0],
            )
            health[human.human] = (hp, max_hp)

        positioned = [
            item for item in relevant if item[0].human in health
        ]
        transform_ranges = [
            (transform_address(human), 32)
            for human, _pointer, _name, _spec, _remaining in positioned
        ]
        transform_rows = client.read_blocks(transform_ranges)

        enemies: list[EnemyReading] = []
        for item, transform_raw in zip(positioned, transform_rows, strict=True):
            human, _gang_pointer, gang_name, spec, remaining = item
            hp, max_hp = health[human.human]
            # Once a native gang has removed the actor from its member list,
            # its old Brain slot may remain stale for a moment.  Requiring the
            # exact live handle prevents a dead/exiting actor from being shown
            # under stale membership.
            members = struct.unpack_from(
                f"<{GANG_MEMBER_CAPACITY}I",
                gang_raw[gang_pointers[human.human]],
                GANG_MEMBER_ARRAY_OFFSET,
            )
            if human.handle not in members:
                continue
            position = decode_position(transform_raw)
            enemies.append(
                EnemyReading(
                    human=human.human,
                    handle=human.handle,
                    actor_name=human.name,
                    gang_name=gang_name,
                    gang_label=spec.label,
                    gang_color=display_gang_color(spec, remaining),
                    remaining=remaining,
                    origin=classify_origin(human.name, spec),
                    counts_for_c3=spec.counts_for_c3,
                    hp=hp,
                    max_hp=max_hp,
                    position=position,
                )
            )

        camera = None
        try:
            camera_transform = resolve_camera_transform(client)
            camera_raw = client.read_block(camera_transform, 0x60, progress=False)
            camera = decode_camera(camera_raw)
        except (RuntimeError, ValueError, struct.error):
            camera = None

        enemies.sort(key=lambda actor: (actor.gang_label, actor.handle))
        gangs = tuple(sorted(gangs_by_name.values(), key=lambda gang: gang.label))
        return WorldSnapshot(
            message="ONLINE · read only · native gang",
            enemies=tuple(enemies),
            gangs=gangs,
            camera=camera,
        )


class GangWorker(threading.Thread):
    def __init__(self, *, port: int, sample_hz: float) -> None:
        super().__init__(name="PayinGangWorker", daemon=True)
        self.port = port
        self.sample_hz = sample_hz
        self.reader = WorldReader()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = WorldSnapshot()

    def snapshot(self) -> WorldSnapshot:
        with self.lock:
            return self.latest

    def publish(self, snapshot: WorldSnapshot) -> None:
        with self.lock:
            self.latest = snapshot

    def run_connection(self, client: PineClient) -> None:
        verify_game(client)
        interval = 1.0 / self.sample_hz
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                snapshot = self.reader.read(client)
            except (RuntimeError, ValueError, struct.error) as error:
                snapshot = replace(
                    self.snapshot(),
                    message=f"Resolving again after loading: {error}",
                    enemies=(),
                    gangs=(),
                    camera=None,
                )
            self.publish(snapshot)
            elapsed = time.perf_counter() - started
            self.stop_event.wait(max(0.0, interval - elapsed))

    def run(self) -> None:
        while not self.stop_event.is_set():
            client: PineClient | None = None
            try:
                current = self.snapshot()
                self.publish(replace(current, message="Connecting to PCSX2..."))
                client = PineClient(port=self.port, timeout=0.9)
                self.run_connection(client)
            except Exception as error:
                current = self.snapshot()
                self.publish(
                    WorldSnapshot(
                        message=f"Waiting for game: {error}",
                        reconnects=current.reconnects + 1,
                    )
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


@dataclass(frozen=True)
class ProjectedEnemy:
    actor: EnemyReading
    anchor_x: float
    anchor_y: float
    label_x: float
    label_y: float


class GangOverlay:
    LABEL_WIDTH = 210
    LABEL_HEIGHT = 38

    def __init__(
        self,
        root: tk.Tk,
        *,
        worker: GangWorker,
        x_offset: int,
        y_offset: int,
        opacity: float,
    ) -> None:
        self.root = root
        self.worker = worker
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.opacity = opacity
        self.game_window = 0
        self.geometry: tuple[int, int, int, int] | None = None
        self.quit_down = False
        self.closed = False

        root.title("Payin The Cost Gang Overlay")
        root.overrideredirect(True)
        root.configure(bg=COLOR_KEY)
        root.attributes("-topmost", True)
        root.attributes("-alpha", opacity)
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

    @staticmethod
    def dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        return sum(left * right for left, right in zip(a, b))

    @classmethod
    def project_world(
        cls,
        point: tuple[float, float, float],
        camera: tuple[
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
            float,
            float,
        ],
        width: int,
        height: int,
    ) -> tuple[float, float] | None:
        position, right, forward, up, horizontal_slope, vertical_slope = camera
        relative = tuple(point[index] - position[index] for index in range(3))
        camera_point = (
            cls.dot(relative, right),
            cls.dot(relative, forward),
            cls.dot(relative, up),
        )
        if camera_point[1] <= 0.08:
            return None
        focal_x = width / (2.0 * horizontal_slope)
        focal_y = height / (2.0 * vertical_slope)
        x = width * 0.5 + camera_point[0] / camera_point[1] * focal_x
        y = height * 0.5 - camera_point[2] / camera_point[1] * focal_y
        if not (-120.0 < x < width + 120.0 and -80.0 < y < height + 80.0):
            return None
        return x, y

    @staticmethod
    def intersects(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
        margin: float = 3.0,
    ) -> bool:
        return not (
            first[2] + margin <= second[0]
            or second[2] + margin <= first[0]
            or first[3] + margin <= second[1]
            or second[3] + margin <= first[1]
        )

    def project_enemies(
        self,
        snapshot: WorldSnapshot,
        width: int,
        height: int,
    ) -> list[ProjectedEnemy]:
        if snapshot.camera is None:
            return []
        anchors: list[tuple[EnemyReading, float, float]] = []
        for actor in snapshot.enemies:
            point = (
                actor.position[0],
                actor.position[1],
                actor.position[2] + 2.15,
            )
            projected = self.project_world(
                point, snapshot.camera, width, height
            )
            if projected is not None:
                anchors.append((actor, *projected))

        # Place close actors in nearby lanes so every label remains readable.
        anchors.sort(key=lambda item: (item[2], item[1]))
        occupied: list[tuple[float, float, float, float]] = []
        result: list[ProjectedEnemy] = []
        shifts = (0, -42, 42, -84, 84, -126, 126, -168, 168)
        half_width = self.LABEL_WIDTH * 0.5
        for actor, anchor_x, anchor_y in anchors:
            label_x = min(max(anchor_x, half_width + 4), width - half_width - 4)
            chosen_y = anchor_y - 25
            for shift in shifts:
                candidate_y = min(
                    max(anchor_y - 25 + shift, self.LABEL_HEIGHT * 0.5 + 4),
                    height - self.LABEL_HEIGHT * 0.5 - 4,
                )
                rectangle = (
                    label_x - half_width,
                    candidate_y - self.LABEL_HEIGHT * 0.5,
                    label_x + half_width,
                    candidate_y + self.LABEL_HEIGHT * 0.5,
                )
                if not any(self.intersects(rectangle, old) for old in occupied):
                    chosen_y = candidate_y
                    occupied.append(rectangle)
                    break
            else:
                chosen_y = min(
                    max(anchor_y - 25, self.LABEL_HEIGHT * 0.5 + 4),
                    height - self.LABEL_HEIGHT * 0.5 - 4,
                )
            result.append(
                ProjectedEnemy(actor, anchor_x, anchor_y, label_x, chosen_y)
            )
        return result

    def draw_enemy_label(self, projected: ProjectedEnemy) -> None:
        actor = projected.actor
        x = projected.label_x
        y = projected.label_y
        half_width = self.LABEL_WIDTH * 0.5
        half_height = self.LABEL_HEIGHT * 0.5
        self.canvas.create_line(
            projected.anchor_x,
            projected.anchor_y,
            x,
            y + half_height,
            fill=actor.gang_color,
            width=1,
        )
        self.canvas.create_oval(
            projected.anchor_x - 2,
            projected.anchor_y - 2,
            projected.anchor_x + 2,
            projected.anchor_y + 2,
            fill=actor.gang_color,
            outline="",
        )
        self.canvas.create_rectangle(
            x - half_width,
            y - half_height,
            x + half_width,
            y + half_height,
            fill=LABEL_BG,
            outline=actor.gang_color,
            width=2,
        )
        count_note = (
            f"{actor.remaining} remaining"
            if actor.counts_for_c3
            else f"{actor.remaining} remaining · DOES NOT COUNT"
        )
        self.canvas.create_text(
            x,
            y - 8,
            text=f"{actor.gang_label} · {count_note}",
            fill=actor.gang_color,
            font=("Segoe UI", 9, "bold"),
        )
        origin_color = RED if actor.origin == "RESSURECT" else TEXT
        if not actor.counts_for_c3:
            origin_color = DIM if "C2" in actor.origin else RED
        elif actor.remaining <= 2:
            origin_color = RETIRING_GRAY
        self.canvas.create_text(
            x,
            y + 9,
            text=actor.origin,
            fill=origin_color,
            font=("Segoe UI", 9, "bold"),
        )

    def draw_panel(self, snapshot: WorldSnapshot) -> None:
        left = self.x_offset
        top = self.y_offset
        width = 510
        active = aggregate_panel_gangs(snapshot.gangs)
        rows = max(1, math.ceil(len(active) / 4))
        height = 70 + rows * 18
        self.canvas.create_rectangle(
            left,
            top,
            left + width,
            top + height,
            fill=PANEL_BG,
            outline=PANEL_BORDER,
            width=2,
        )
        self.canvas.create_text(
            left + 12,
            top + 10,
            anchor="nw",
            text="PAYIN' THE COST · GANGS C3",
            fill=CYAN,
            font=("Consolas", 12, "bold"),
        )
        self.canvas.create_text(
            left + width - 12,
            top + 12,
            anchor="ne",
            text="Ctrl+Shift+Q exit",
            fill=DIM,
            font=("Consolas", 8, "normal"),
        )
        self.canvas.create_text(
            left + 12,
            top + 35,
            anchor="nw",
            text=snapshot.message[:72],
            fill=GREEN if snapshot.camera is not None else YELLOW,
            font=("Consolas", 9, "normal"),
        )
        if not active:
            detail = (
                "Waiting for C3 gangs; inherited C2 members do not count."
            )
            self.canvas.create_text(
                left + 12,
                top + 57,
                anchor="nw",
                text=detail,
                fill=DIM,
                font=("Consolas", 8, "normal"),
            )
            return
        for index, gang in enumerate(active):
            column = index % 4
            row = index // 4
            x = left + 12 + column * 121
            y = top + 58 + row * 18
            self.canvas.create_text(
                x,
                y,
                anchor="nw",
                text=f"{gang.label}: {gang.remaining}",
                fill=gang.color,
                font=("Consolas", 8, "bold"),
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
        screen_x, screen_y, width, height = current
        if current != self.geometry:
            self.geometry = current
            self.root.geometry(f"{width}x{height}+{screen_x}+{screen_y}")
            self.canvas.configure(width=width, height=height)
        self.canvas.delete("all")
        snapshot = self.worker.snapshot()
        for projected in self.project_enemies(snapshot, width, height):
            self.draw_enemy_label(projected)
        self.draw_panel(snapshot)
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


def print_once(port: int) -> int:
    client = PineClient(port=port, timeout=1.0)
    try:
        verify_game(client)
        snapshot = WorldReader().read(client)
    finally:
        client.close()
    print(snapshot.message)
    if not snapshot.enemies:
        print("No relevant enemies are loaded")
        return 2
    for actor in snapshot.enemies:
        x, y, z = actor.position
        progress = "COUNTS FOR C3" if actor.counts_for_c3 else "DOES NOT COUNT"
        print(
            f"0x{actor.handle:08X} {actor.actor_name:<16} | "
            f"{actor.gang_label:<14} {actor.remaining:2d} remaining | "
            f"{actor.origin:<19} | {progress:<9} | "
            f"HP {actor.hp:3d}/{actor.max_hp:<3d} "
            f"pos=({x:.2f},{y:.2f},{z:.2f})"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only gang/origin overlay for each enemy in Payin' The Cost"
        )
    )
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--sample-hz", type=float, default=12.0)
    parser.add_argument("--x-offset", type=int, default=18)
    parser.add_argument("--y-offset", type=int, default=18)
    parser.add_argument("--opacity", type=float, default=0.70)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 2.0 <= args.sample_hz <= 30.0:
        raise ValueError("--sample-hz must be between 2 and 30")
    if not 0.25 <= args.opacity <= 1.0:
        raise ValueError("--opacity must be between 0.25 and 1.0")

    instance_handle = acquire_single_instance()
    if not instance_handle:
        if args.once:
            print("The overlay is already running and using PINE")
            raise SystemExit(3)
        return
    try:
        if args.once:
            raise SystemExit(print_once(args.port))
        worker = GangWorker(port=args.port, sample_hz=args.sample_hz)
        worker.start()
        root = tk.Tk()
        overlay = GangOverlay(
            root,
            worker=worker,
            x_offset=args.x_offset,
            y_offset=args.y_offset,
            opacity=args.opacity,
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
