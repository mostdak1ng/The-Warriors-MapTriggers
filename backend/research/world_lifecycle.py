#!/usr/bin/env python3
"""World lifetime checks for The Warriors (SLUS-21215).

Mission loads temporarily remove the logical camera, player entity, and
collision world.  Heap allocation/free hooks are only safe after all three
have returned and remained coherent for a short stability window. An optional
non-allocating heartbeat also verifies that the bootstrap callsite executes;
Game Over can retain a complete world while suspending that pass.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import time
from pathlib import Path

from collision_render_probe import COLLISION_GLOBAL, COLLISION_VTABLE
from freecam_probe import (
    CAMERA_OBJECT_POINTER,
    CAMERA_TRANSFORM_OFFSET,
    CAMERA_VTABLE,
    resolve_camera_transform,
)
from pine_client import GAME_ID, PineClient
from ingame_volume_probe import probe_heap_callsite
from player_noclip import resolve_player_object


@dataclass(frozen=True)
class WorldIdentity:
    camera_object: int
    camera_transform: int
    camera_vtable: int
    player_object: int
    collision_object: int
    collision_triangles: int
    collision_vertices: int
    collision_vertex_pointer: int


def read_world_identity(
    client: PineClient,
    *,
    require_follow_camera: bool = False,
) -> WorldIdentity:
    """Return one coherent gameplay-world snapshot or raise RuntimeError."""
    if client.text(GAME_ID) != "SLUS-21215":
        raise RuntimeError("The Warriors SLUS-21215 is not running")

    camera_transform = resolve_camera_transform(client)
    camera_object = camera_transform - CAMERA_TRANSFORM_OFFSET
    camera_vtable = client.read(camera_object, 32)
    if require_follow_camera and camera_vtable != CAMERA_VTABLE:
        raise RuntimeError(
            "normal follow camera is not active yet: "
            f"0x{camera_vtable:08X}"
        )
    player_object = resolve_player_object(client)

    collision_object = client.read(COLLISION_GLOBAL, 32)
    if not 0x00700000 <= collision_object < 0x02000000:
        raise RuntimeError(
            f"implausible collision object: 0x{collision_object:08X}"
        )
    if client.read(collision_object, 32) != COLLISION_VTABLE:
        raise RuntimeError(
            f"collision vtable mismatch at 0x{collision_object:08X}"
        )
    collision_triangles = client.read(collision_object + 0x84, 16)
    collision_vertex_pointer = client.read(collision_object + 0x8C, 32)
    collision_vertices = client.read(collision_object + 0x90, 32)
    if not 0 < collision_triangles < 0x10000:
        raise RuntimeError(
            f"implausible collision triangle count: {collision_triangles}"
        )
    if not 0 < collision_vertices < 0x100000:
        raise RuntimeError(
            f"implausible collision vertex count: {collision_vertices}"
        )
    if not 0x00700000 <= collision_vertex_pointer < 0x02000000:
        raise RuntimeError(
            "implausible collision vertex pointer: "
            f"0x{collision_vertex_pointer:08X}"
        )

    return WorldIdentity(
        camera_object=camera_object,
        camera_transform=camera_transform,
        camera_vtable=camera_vtable,
        player_object=player_object,
        collision_object=collision_object,
        collision_triangles=collision_triangles,
        collision_vertices=collision_vertices,
        collision_vertex_pointer=collision_vertex_pointer,
    )


def wait_for_world_ready(
    client: PineClient,
    *,
    timeout: float = 0.0,
    stable_duration: float = 1.0,
    poll_interval: float = 0.05,
    require_follow_camera: bool = False,
    stop_file: Path | None = None,
    panel_hotkeys=None,
    require_heap_callsite: bool = False,
) -> WorldIdentity:
    """Wait for stable world identity and optionally the allocator pass.

    Identity checks are read-only. The optional heartbeat temporarily hooks
    the existing callsite without allocating or freeing memory.
    """
    if timeout < 0 or stable_duration < 0:
        raise ValueError("world wait timeout and stability cannot be negative")
    if poll_interval <= 0:
        raise ValueError("world wait poll interval must be positive")

    deadline = time.monotonic() + timeout if timeout else math.inf
    candidate: WorldIdentity | None = None
    candidate_since = 0.0
    last_notice = ""
    next_heap_probe = 0.0
    heap_wait_announced = False
    while True:
        from session_guard import parent_alive
        if not parent_alive():
            if stop_file is not None:
                stop_file.touch()
            raise InterruptedError('launcher exited')
        if panel_hotkeys is not None and panel_hotkeys.poll():
            if stop_file is not None:
                stop_file.touch()
            raise InterruptedError("world wait cancelled")
        if stop_file is not None and stop_file.exists():
            raise InterruptedError("world wait cancelled")
        now = time.monotonic()
        try:
            current = read_world_identity(
                client,
                require_follow_camera=require_follow_camera,
            )
            if current != candidate:
                candidate = current
                candidate_since = now
                next_heap_probe = 0.0
                heap_wait_announced = False
            if now - candidate_since >= stable_duration:
                if not require_heap_callsite:
                    return current
                if now >= next_heap_probe:
                    callsite_ready = probe_heap_callsite(client, timeout=0.1,
                        on_wait=panel_hotkeys.poll if panel_hotkeys is not None else None)
                    next_heap_probe = time.monotonic() + 1.0
                    if callsite_ready:
                        # A load may have begun during the heartbeat.
                        verified = read_world_identity(
                            client, require_follow_camera=require_follow_camera
                        )
                        if verified == current:
                            return current
                        candidate = None
                    elif not heap_wait_announced:
                        controls = (
                            " O: hide/show game 2D; F10: exit."
                            if panel_hotkeys is not None else ""
                        )
                        print(
                            "World is loaded, but the allocator pass is inactive "
                            "(for example, Game Over). Waiting before renderer "
                            "installation." + controls,
                            flush=True,
                        )
                        heap_wait_announced = True
            last_notice = ""
        except RuntimeError as error:
            candidate = None
            candidate_since = 0.0
            notice = str(error)
            if notice != last_notice:
                print(
                    f"world unavailable; waiting read-only: {notice}",
                    flush=True,
                )
                last_notice = notice
        if now >= deadline:
            raise TimeoutError(
                f"gameplay world did not stabilize within {timeout:g} seconds"
            )
        time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28011)
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("--stable-seconds", type=float, default=1.0)
    parser.add_argument(
        "--require-follow-camera",
        action="store_true",
        help="wait through cinematics until the normal gameplay camera is active",
    )
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--ui-panels-state", type=Path)
    parser.add_argument(
        "--require-heap-callsite", action="store_true",
        help="verify the allocator pass with a temporary non-allocating heartbeat",
    )
    args = parser.parse_args()

    client = PineClient(port=args.port)
    panel_hotkeys = None
    try:
        if args.ui_panels_state is not None:
            from game_ui_panels import PanelHotkeys

            panel_hotkeys = PanelHotkeys(client, args.ui_panels_state)
        world = wait_for_world_ready(
            client,
            timeout=args.timeout,
            stable_duration=args.stable_seconds,
            require_follow_camera=args.require_follow_camera,
            stop_file=args.stop_file,
            panel_hotkeys=panel_hotkeys,
            require_heap_callsite=args.require_heap_callsite,
        )
    except __import__("ingame_volume_probe").BootstrapCancelled as error:
        print(f"WARNING: {error}", flush=True)
        raise SystemExit(79)
    except InterruptedError:
        raise SystemExit(76)
    finally:
        try:
            if panel_hotkeys is not None:
                panel_hotkeys.close()
        finally:
            client.close()
    print(
        "world-ready "
        f"camera=0x{world.camera_object:08X} "
        f"vtable=0x{world.camera_vtable:08X} "
        f"player=0x{world.player_object:08X} "
        f"collision=0x{world.collision_object:08X} "
        f"triangles={world.collision_triangles} "
        f"vertices={world.collision_vertices}",
        flush=True,
    )


if __name__ == "__main__":
    main()
