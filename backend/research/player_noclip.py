#!/usr/bin/env python3
"""Resolve and carry the live player with the same transforms used by gameplay.

Normal humans publish their transform through a 32-byte WarMoveInstance slot.
Some special states instead expose human+0x2C0.  The game's own teleport path
keeps both representations synchronized, so the noclip controller mirrors the
position into all live representations while it is attached.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from pine_client import PineClient


PLAYER_MANAGER_POINTER = 0x0051489C
ENTITY_REGISTRY = 0x006EBD38
MOVE_TRANSFORM_BASE = 0x00714B00
MOVE_TRANSFORM_STRIDE = 0x20

HUMAN_MOVE_INDEX_OFFSET = 0x92
HUMAN_STATE_POINTER_OFFSET = 0xD4
HUMAN_OBJECT_TRANSFORM_OFFSET = 0x10
HUMAN_LOCAL_POSITION_OFFSET = 0x2C0
# Both fields accumulate the same downward speed while gameplay gravity runs.
# Keeping them at zero while attached prevents a deferred high-speed fall.
HUMAN_VERTICAL_SPEED_OFFSETS = (0x38, 0x3A0)

# HuGetPosition returns human+0x2C0 when either condition is active.
LOCAL_POSITION_FLAGS64 = 0x0000001C00000000
LOCAL_POSITION_FLAGS32 = 0x00000040

# Exact bits used by HuSetGodMode and HuSetNoTarget.
PLAYER_GOD_MODE_BIT = 0x0000000000000010
PLAYER_NO_TARGET_BIT = 0x0000100000000000
PLAYER_SAFETY_MASK = PLAYER_GOD_MODE_BIT | PLAYER_NO_TARGET_BIT


def plausible_ee_pointer(pointer: int) -> bool:
    return 0x00600000 <= pointer < 0x02000000


def resolve_player_object(client: PineClient) -> int:
    manager = client.read(PLAYER_MANAGER_POINTER, 32)
    if not plausible_ee_pointer(manager):
        raise RuntimeError(f"implausible player manager: 0x{manager:08X}")
    handle = client.read(manager + 0x228, 32)
    if handle == 0xFFFFFFFF:
        raise RuntimeError("player zero has no live entity handle")
    generation = handle & 0xFFFF
    index = handle >> 16
    registry_entry = ENTITY_REGISTRY + index * 8
    if client.read(registry_entry + 4, 16) != generation:
        raise RuntimeError("player zero entity handle generation mismatch")
    human = client.read(registry_entry, 32)
    if not plausible_ee_pointer(human):
        raise RuntimeError(f"implausible player object: 0x{human:08X}")
    return human


def move_transform_address(client: PineClient, human: int) -> int | None:
    move_index = client.read(human + HUMAN_MOVE_INDEX_OFFSET, 16)
    if move_index & 0x8000:
        move_index -= 0x10000
    if not 0 <= move_index < 0x4000:
        return None
    address = MOVE_TRANSFORM_BASE + move_index * MOVE_TRANSFORM_STRIDE
    if address > 0x01FFFFFF - MOVE_TRANSFORM_STRIDE:
        return None
    return address


def uses_local_position(client: PineClient, human: int) -> bool:
    state = client.read(human + HUMAN_STATE_POINTER_OFFSET, 32)
    if not plausible_ee_pointer(state):
        return False
    flags64 = client.read(state, 64)
    flags32 = client.read(state + 8, 32)
    return bool(
        flags64 & LOCAL_POSITION_FLAGS64
        or flags32 & LOCAL_POSITION_FLAGS32
    )


def effective_position_address(client: PineClient, human: int) -> int:
    if uses_local_position(client, human):
        return human + HUMAN_LOCAL_POSITION_OFFSET
    movement = move_transform_address(client, human)
    if movement is None:
        raise RuntimeError("player has no valid WarMoveInstance transform")
    return movement


def read_player_position(
    client: PineClient, human: int | None = None
) -> tuple[int, tuple[float, float, float]]:
    human = resolve_player_object(client) if human is None else human
    address = effective_position_address(client, human)
    position = struct.unpack(
        "<3f", client.read_block(address, 16, progress=False)[:12]
    )
    if not all(math.isfinite(component) for component in position):
        raise RuntimeError("player position contains non-finite values")
    return human, position


@dataclass
class SavedPlayerPosition:
    human: int
    movement_address: int | None
    movement: bytes | None
    object_position: bytes
    local_position: bytes


class PlayerNoclipController:
    """Continuously mirror a camera-relative position into the live player."""

    def __init__(
        self,
        client: PineClient,
        *,
        offset_forward: float = 3.0,
        offset_up: float = -1.5,
        safety: bool = False,
        god_mode: bool | None = None,
        no_target: bool | None = None,
        attached: bool = True,
        neutralize_gravity: bool = True,
        restore_position_on_close: bool = False,
    ):
        self.client = client
        self.offset_forward = offset_forward
        self.offset_up = offset_up
        self.safety = safety
        self.manage_god_mode = safety or god_mode is not None
        self.manage_no_target = safety or no_target is not None
        self.god_mode = safety if god_mode is None else god_mode
        self.no_target = safety if no_target is None else no_target
        self.neutralize_gravity = neutralize_gravity
        self.restore_position_on_close = restore_position_on_close
        self.attached = attached
        self.human = 0
        self.movement_address: int | None = None
        self.original_safety_bits = 0
        self.saved_position: SavedPlayerPosition | None = None
        self.writes = 0
        self.gravity_resets = 0
        self.refresh()

    def _neutralize_vertical_speed(self) -> None:
        if not self.neutralize_gravity or not self.human:
            return
        for offset in HUMAN_VERTICAL_SPEED_OFFSETS:
            self.client.write(self.human + offset, 0, 32)
        self.gravity_resets += 1

    def _restore_safety(self) -> None:
        if (
            not (self.manage_god_mode or self.manage_no_target)
            or not self.human
        ):
            return
        current = self.client.read(self.human + 0xE0, 64)
        managed_mask = 0
        if self.manage_god_mode:
            managed_mask |= PLAYER_GOD_MODE_BIT
        if self.manage_no_target:
            managed_mask |= PLAYER_NO_TARGET_BIT
        restored = (current & ~managed_mask) | (
            self.original_safety_bits & managed_mask
        )
        if restored != current:
            self.client.write(self.human + 0xE0, restored, 64)

    def _apply_runtime_safety(self) -> None:
        if (
            not (self.manage_god_mode or self.manage_no_target)
            or not self.human
        ):
            return
        current = self.client.read(self.human + 0xE0, 64)
        target = current
        for managed, enabled, bit in (
            (
                self.manage_god_mode,
                self.god_mode,
                PLAYER_GOD_MODE_BIT,
            ),
            (
                self.manage_no_target,
                self.no_target,
                PLAYER_NO_TARGET_BIT,
            ),
        ):
            if not managed:
                continue
            if enabled:
                target |= bit
            else:
                target = (
                    (target & ~bit)
                    | (self.original_safety_bits & bit)
                )
        if target != current:
            self.client.write(self.human + 0xE0, target, 64)

    def refresh(self) -> None:
        human = resolve_player_object(self.client)
        if human == self.human:
            self.movement_address = move_transform_address(
                self.client, human
            )
            self._apply_runtime_safety()
            return
        if self.human:
            self._restore_safety()
        self.human = human
        self.movement_address = move_transform_address(self.client, human)
        self.original_safety_bits = (
            self.client.read(human + 0xE0, 64) & PLAYER_SAFETY_MASK
        )
        if self.saved_position is None:
            movement = (
                self.client.read_block(
                    self.movement_address, 16, progress=False
                )
                if self.movement_address is not None
                else None
            )
            self.saved_position = SavedPlayerPosition(
                human=human,
                movement_address=self.movement_address,
                movement=movement,
                object_position=self.client.read_block(
                    human + HUMAN_OBJECT_TRANSFORM_OFFSET,
                    16,
                    progress=False,
                ),
                local_position=self.client.read_block(
                    human + HUMAN_LOCAL_POSITION_OFFSET,
                    16,
                    progress=False,
                ),
            )
        self._apply_runtime_safety()

    def toggle(self) -> bool:
        return self.set_attached(not self.attached)

    def set_attached(self, enabled: bool) -> bool:
        if self.attached and not enabled:
            # Remove velocity accumulated between the last position update and
            # this key edge before returning control to normal physics.
            self._neutralize_vertical_speed()
            self.attached = False
        elif not self.attached and enabled:
            self.attached = True
            self.refresh()
            self._neutralize_vertical_speed()
        return self.attached

    def set_god_mode(self, enabled: bool) -> bool:
        if not self.manage_god_mode:
            raise RuntimeError("god mode is not managed by this controller")
        self.god_mode = enabled
        self.refresh()
        return self.god_mode

    def toggle_god_mode(self) -> bool:
        return self.set_god_mode(not self.god_mode)

    def set_no_target(self, enabled: bool) -> bool:
        if not self.manage_no_target:
            raise RuntimeError("no-target is not managed by this controller")
        self.no_target = enabled
        self.refresh()
        return self.no_target

    def target_position(
        self,
        camera_position: tuple[float, float, float] | list[float],
        camera_forward: tuple[float, float, float] | list[float],
    ) -> tuple[float, float, float]:
        return tuple(
            camera_position[axis]
            + camera_forward[axis] * self.offset_forward
            + (self.offset_up if axis == 2 else 0.0)
            for axis in range(3)
        )  # type: ignore[return-value]

    def write(
        self,
        camera_position: tuple[float, float, float] | list[float],
        camera_forward: tuple[float, float, float] | list[float],
    ) -> tuple[float, float, float]:
        self.refresh()
        target = self.target_position(camera_position, camera_forward)
        if not all(math.isfinite(component) for component in target):
            raise RuntimeError("noclip target contains non-finite values")
        block = struct.pack("<4f", *target, 1.0)
        if self.movement_address is not None:
            self.client.write_block(self.movement_address, block)
        self.client.write_block(
            self.human + HUMAN_OBJECT_TRANSFORM_OFFSET, block
        )
        self.client.write_block(
            self.human + HUMAN_LOCAL_POSITION_OFFSET, block
        )
        self._neutralize_vertical_speed()
        self.writes += 1
        return target

    def close(self) -> None:
        try:
            if self.attached or self.restore_position_on_close:
                self._neutralize_vertical_speed()
            if self.restore_position_on_close and self.saved_position is not None:
                saved = self.saved_position
                if saved.movement_address is not None and saved.movement is not None:
                    self.client.write_block(
                        saved.movement_address, saved.movement
                    )
                self.client.write_block(
                    saved.human + HUMAN_OBJECT_TRANSFORM_OFFSET,
                    saved.object_position,
                )
                self.client.write_block(
                    saved.human + HUMAN_LOCAL_POSITION_OFFSET,
                    saved.local_position,
                )
        finally:
            self._restore_safety()
