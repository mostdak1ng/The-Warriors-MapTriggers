#!/usr/bin/env python3
"""Discover and sample live physics bounds.

The Warriors keeps several persistent TPhysicsBound-derived objects per human.
Their vtables and layouts are stable while their endpoints follow animation:

* type 2 / sphere: center at +0x50, radius at +0x40
* type 3 / principal body bound: origin at +0x50, radius/height at +0x40
* type 4 / capsule: endpoints at +0x40/+0x50, radius at +0x60

The adjacent type-1 vtable is an oriented box used by live objects.  It keeps
three half extents at +0x40 and a row-major orthonormal transform at +0x50;
the fourth row is the world-space center.  Type 1 is sampled separately so the
accepted player-bound association remains unchanged.  The raw OBB inventory
is intentionally broader than gameplay climbability.  Confirmed dynamic climb
families are selected through their native owner vtable and bound-pointer
layout; the currently validated family is Car.

Names such as hurtbox/attack box remain deliberately unassigned.  The sampler
reports the native type and geometry so gameplay tests can establish semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import time

from pine_client import PineClient


SCAN_START = 0x00600000
SCAN_END = 0x01400000
PAGE_SIZE = 0x10000
BOUND_VTABLE_OFFSET = 0x34

SHAPE_SPHERE = 2
SHAPE_PRINCIPAL = 3
SHAPE_CAPSULE = 4
OBB_VTABLE = 0x005447F0
CAR_VTABLE = 0x00544C08
CAR_OBB_POINTER_OFFSETS = (0x11E4, 0x11E8)
DYNAMIC_CLIMB_OBJECT_LAYOUTS = {
    CAR_VTABLE: CAR_OBB_POINTER_OFFSETS,
}
SHAPE_VTABLES = {
    0x00544830: SHAPE_SPHERE,
    0x005447B0: SHAPE_PRINCIPAL,
    0x00544770: SHAPE_CAPSULE,
}
SHAPE_NAMES = {
    SHAPE_SPHERE: "sphere/type-2",
    SHAPE_PRINCIPAL: "principal/type-3",
    SHAPE_CAPSULE: "capsule/type-4",
}


@dataclass(frozen=True)
class PlayerDebugShape:
    kind: int
    address: int
    flags: int
    first: tuple[float, float, float]
    second: tuple[float, float, float]
    radius: float


@dataclass(frozen=True)
class PhysicsObb:
    address: int
    owner: int
    flags: int
    half_extents: tuple[float, float, float]
    axes: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    center: tuple[float, float, float]
    distance: float


def finite_vector(vector: tuple[float, float, float]) -> bool:
    return all(math.isfinite(component) for component in vector)


class PhysicsShapeSampler:
    """Cache bound-object addresses, then sample their changing geometry."""

    def __init__(self, client: PineClient):
        self.client = client
        self.objects_by_page: dict[int, list[tuple[int, int]]] = {}
        self.obb_objects_by_page: dict[int, list[int]] = {}
        self.obb_addresses: set[int] = set()
        self.climb_objects: dict[int, int] = {}
        self.tracked_objects: dict[int, tuple[int, int]] = {}
        self.next_reacquire = 0.0
        self.discovery_reads = 0
        self.sample_reads = 0
        self.discover()

    def discover(self) -> None:
        found: dict[int, list[tuple[int, int]]] = {}
        found_obbs: dict[int, list[int]] = {}
        found_climb_objects: dict[int, int] = {}
        for page in range(SCAN_START, SCAN_END, PAGE_SIZE):
            raw = self.client.read_block(
                page, PAGE_SIZE, progress=False
            )
            self.discovery_reads += 1
            words = struct.unpack(f"<{PAGE_SIZE // 4}I", raw)
            for index, value in enumerate(words):
                word_address = page + index * 4
                climb_offsets = DYNAMIC_CLIMB_OBJECT_LAYOUTS.get(value)
                if (
                    climb_offsets is not None
                    and word_address
                    <= SCAN_END - max(climb_offsets) - 4
                ):
                    found_climb_objects[word_address] = value
                kind = SHAPE_VTABLES.get(value)
                is_obb = value == OBB_VTABLE
                if kind is None and not is_obb:
                    continue
                address = word_address - BOUND_VTABLE_OFFSET
                if not SCAN_START <= address <= SCAN_END - 0x70:
                    continue
                object_page = address & ~(PAGE_SIZE - 1)
                if is_obb:
                    found_obbs.setdefault(object_page, []).append(address)
                else:
                    assert kind is not None
                    found.setdefault(object_page, []).append((address, kind))
        self.objects_by_page = found
        self.obb_objects_by_page = found_obbs
        self.obb_addresses = {
            address
            for addresses in found_obbs.values()
            for address in addresses
        }
        self.climb_objects = found_climb_objects

    @staticmethod
    def _decode_obb(
        raw: bytes,
        offset: int,
        address: int,
        point: tuple[float, float, float],
    ) -> PhysicsObb | None:
        if not 0 <= offset <= len(raw) - 0x90:
            return None
        if struct.unpack_from("<I", raw, offset + 0x34)[0] != OBB_VTABLE:
            return None
        kind, owner, flags = struct.unpack_from("<BBH", raw, offset + 0x30)
        if kind != 1 or not flags & 0x0002:
            # Constructors leave unused pool entries at flags=1.  Every live
            # populated type-1 bound observed in gameplay carries bit 1.
            return None
        half_extents = struct.unpack_from("<3f", raw, offset + 0x40)
        axes = tuple(
            struct.unpack_from("<3f", raw, offset + component_offset)
            for component_offset in (0x50, 0x60, 0x70)
        )
        center = struct.unpack_from("<3f", raw, offset + 0x80)
        if (
            not finite_vector(half_extents)
            or not finite_vector(center)
            or any(not finite_vector(axis) for axis in axes)
            or min(half_extents) <= 0.0
            or max(half_extents) > 100.0
        ):
            return None
        for axis in axes:
            length = math.sqrt(sum(component * component for component in axis))
            if not 0.98 <= length <= 1.02:
                return None
        for first in range(3):
            for second in range(first + 1, 3):
                dot = sum(
                    axes[first][component] * axes[second][component]
                    for component in range(3)
                )
                if abs(dot) > 0.03:
                    return None

        delta = tuple(point[index] - center[index] for index in range(3))
        outside = []
        for axis, half_extent in zip(axes, half_extents):
            local = abs(
                sum(delta[index] * axis[index] for index in range(3))
            )
            outside.append(max(local - half_extent, 0.0))
        distance = math.sqrt(sum(component * component for component in outside))
        return PhysicsObb(
            address=address,
            owner=owner,
            flags=flags,
            half_extents=half_extents,
            axes=axes,
            center=center,
            distance=distance,
        )

    def sample_nearest_obb(
        self,
        point: tuple[float, float, float],
        *,
        maximum_distance: float = 35.0,
        minimum_half_extent: float = 0.1,
    ) -> PhysicsObb | None:
        """Return the nearest populated type-1 OBB to a world-space point."""
        rows = self.sample_nearby_obbs(
            point,
            maximum_distance=maximum_distance,
            minimum_half_extent=minimum_half_extent,
            limit=1,
        )
        return rows[0] if rows else None

    def sample_nearby_obbs(
        self,
        point: tuple[float, float, float],
        *,
        maximum_distance: float = 35.0,
        minimum_half_extent: float = 0.1,
        limit: int = 32,
    ) -> list[PhysicsObb]:
        """Return the nearest populated type-1 OBBs, sorted by box distance."""
        return self._sample_nearby_obb_inventory(
            self.obb_objects_by_page,
            point,
            maximum_distance=maximum_distance,
            minimum_half_extent=minimum_half_extent,
            limit=limit,
        )

    def _sample_nearby_obb_inventory(
        self,
        inventory: dict[int, list[int]],
        point: tuple[float, float, float],
        *,
        maximum_distance: float,
        minimum_half_extent: float,
        limit: int,
    ) -> list[PhysicsObb]:
        if limit < 0:
            raise ValueError("OBB sample limit cannot be negative")
        if limit == 0:
            return []
        matches: list[PhysicsObb] = []
        for page, objects in inventory.items():
            raw = self.client.read_block(page, PAGE_SIZE, progress=False)
            self.sample_reads += 1
            for address in objects:
                shape = self._decode_obb(
                    raw,
                    address - page,
                    address,
                    point,
                )
                if (
                    shape is None
                    or max(shape.half_extents) < minimum_half_extent
                    or shape.distance > maximum_distance
                ):
                    continue
                matches.append(shape)
        matches.sort(key=lambda shape: (shape.distance, shape.address))
        return matches[:limit]

    def _sample_dynamic_climb_obb_addresses(self) -> set[int]:
        """Resolve OBB pointers owned by still-live confirmed climb classes."""
        requests: set[tuple[int, int]] = set()
        for object_address, vtable in self.climb_objects.items():
            requests.add((object_address & ~7, 8))
            for offset in DYNAMIC_CLIMB_OBJECT_LAYOUTS[vtable]:
                requests.add(((object_address + offset) & ~7, 8))
        ordered_requests = sorted(requests)
        blocks = self.client.read_blocks(ordered_requests)
        self.sample_reads += len(blocks)
        memory = {
            address: raw
            for (address, _size), raw in zip(ordered_requests, blocks)
        }

        def read_word(address: int) -> int:
            aligned = address & ~7
            raw = memory.get(aligned)
            if raw is None:
                return 0
            return struct.unpack_from("<I", raw, address - aligned)[0]

        addresses: set[int] = set()
        for object_address, expected_vtable in self.climb_objects.items():
            if read_word(object_address) != expected_vtable:
                continue
            for offset in DYNAMIC_CLIMB_OBJECT_LAYOUTS[expected_vtable]:
                bound_address = read_word(object_address + offset)
                if bound_address in self.obb_addresses:
                    addresses.add(bound_address)
        return addresses

    def sample_nearby_climb_obbs(
        self,
        point: tuple[float, float, float],
        *,
        maximum_distance: float = 35.0,
        minimum_half_extent: float = 0.1,
        limit: int = 32,
    ) -> list[PhysicsObb]:
        """Return OBBs from confirmed dynamic climb families (currently Car)."""
        addresses = self._sample_dynamic_climb_obb_addresses()
        inventory: dict[int, list[int]] = {}
        for address in addresses:
            page = address & ~(PAGE_SIZE - 1)
            inventory.setdefault(page, []).append(address)
        for page_addresses in inventory.values():
            page_addresses.sort()
        return self._sample_nearby_obb_inventory(
            inventory,
            point,
            maximum_distance=maximum_distance,
            minimum_half_extent=minimum_half_extent,
            limit=limit,
        )

    @staticmethod
    def _belongs_to_player(
        kind: int,
        player: tuple[float, float, float],
        first: tuple[float, float, float],
        second: tuple[float, float, float],
    ) -> bool:
        px, py, pz = player
        points = (first, second)
        horizontal = min(
            math.hypot(point[0] - px, point[1] - py)
            for point in points
        )
        vertical_min = min(point[2] for point in points)
        vertical_max = max(point[2] for point in points)
        if kind == SHAPE_PRINCIPAL:
            return (
                horizontal <= 1.0
                and pz - 0.75 <= first[2] <= pz + 0.75
            )
        return (
            horizontal <= 0.9
            and vertical_max >= pz + 0.35
            and vertical_min <= pz + 3.0
        )

    @staticmethod
    def _decode(
        raw: bytes,
        offset: int,
        address: int,
        expected_kind: int,
    ) -> PlayerDebugShape | None:
        if not 0 <= offset <= len(raw) - 0x70:
            return None
        vtable = struct.unpack_from("<I", raw, offset + 0x34)[0]
        if SHAPE_VTABLES.get(vtable) != expected_kind:
            return None
        flags = struct.unpack_from("<I", raw, offset + 0x30)[0]
        if expected_kind == SHAPE_SPHERE:
            radius = struct.unpack_from("<f", raw, offset + 0x40)[0]
            first = struct.unpack_from("<3f", raw, offset + 0x50)
            second = first
        elif expected_kind == SHAPE_PRINCIPAL:
            radius, height = struct.unpack_from(
                "<2f", raw, offset + 0x40
            )
            first = struct.unpack_from("<3f", raw, offset + 0x50)
            second = (first[0], first[1], first[2] + height)
        else:
            first = struct.unpack_from("<3f", raw, offset + 0x40)
            second = struct.unpack_from("<3f", raw, offset + 0x50)
            radius = struct.unpack_from("<f", raw, offset + 0x60)[0]
        if (
            not 0.02 <= radius <= 4.0
            or not finite_vector(first)
            or not finite_vector(second)
        ):
            return None
        return PlayerDebugShape(
            kind=expected_kind,
            address=address,
            flags=flags,
            first=first,
            second=second,
            radius=radius,
        )

    @staticmethod
    def _candidate_score(
        player: tuple[float, float, float],
        shape: PlayerDebugShape,
    ) -> float:
        px, py, pz = player
        horizontal = math.hypot(
            shape.first[0] - px, shape.first[1] - py
        )
        if shape.kind == SHAPE_SPHERE:
            expected_z = pz + 1.7
        elif shape.kind == SHAPE_CAPSULE:
            expected_z = pz + 1.25
        else:
            expected_z = pz
        return horizontal + abs(shape.first[2] - expected_z) * 0.25

    def _sample_tracked(self) -> list[PlayerDebugShape]:
        tracked = sorted(self.tracked_objects.values())
        ranges: list[tuple[int, int]] = []
        layouts: list[tuple[int, int, int]] = []
        for address, kind in tracked:
            start = address & ~7
            end = (address + 0x70 + 7) & ~7
            ranges.append((start, end - start))
            layouts.append((address, kind, address - start))
        blocks = self.client.read_blocks(ranges)
        self.sample_reads += len(blocks)
        rows: list[PlayerDebugShape] = []
        invalid: list[int] = []
        for raw, (address, kind, offset) in zip(blocks, layouts):
            shape = self._decode(raw, offset, address, kind)
            if shape is None:
                invalid.append(kind)
            else:
                rows.append(shape)
        for kind in invalid:
            self.tracked_objects.pop(kind, None)
        return rows

    def _reacquire_missing(
        self,
        player: tuple[float, float, float],
        rows: list[PlayerDebugShape],
    ) -> None:
        missing = set(SHAPE_NAMES) - set(self.tracked_objects)
        if not missing or time.monotonic() < self.next_reacquire:
            return
        candidates: dict[int, list[PlayerDebugShape]] = {
            kind: [] for kind in missing
        }
        for page, objects in self.objects_by_page.items():
            raw = self.client.read_block(page, PAGE_SIZE, progress=False)
            self.sample_reads += 1
            for address, kind in objects:
                if kind not in missing:
                    continue
                shape = self._decode(
                    raw, address - page, address, kind
                )
                if shape is None or not self._belongs_to_player(
                    kind, player, shape.first, shape.second
                ):
                    continue
                candidates[kind].append(shape)
        for kind, matches in candidates.items():
            if not matches:
                continue
            chosen = min(
                matches,
                key=lambda shape: self._candidate_score(player, shape),
            )
            self.tracked_objects[kind] = (chosen.address, chosen.kind)
            rows.append(chosen)
        self.next_reacquire = time.monotonic() + 0.25

    def sample(
        self, player: tuple[float, float, float]
    ) -> list[PlayerDebugShape]:
        """Follow acquired native objects even when animation moves them away."""
        rows = self._sample_tracked()
        self._reacquire_missing(player, rows)
        rows.sort(key=lambda row: (row.kind, row.address))
        return rows
