import struct

from player_debug_probe import (
    CAR_OBB_POINTER_OFFSETS,
    CAR_VTABLE,
    OBB_VTABLE,
    PAGE_SIZE,
    PhysicsObb,
    PhysicsShapeSampler,
)
from simultaneous_debug_renderer import (
    CLIMB_DYNAMIC_OBB_COLOR,
    DESTINATION_OFFSET,
    EXTENDED_DATA_SIZE,
    FLAG_RENDER_HEADER,
    FLAG_RENDER_HEADER_OFFSET,
    FLAG_RENDER_MAGIC,
    MATERIAL_CLIMB_FENCE,
    OBB_CAPACITY,
    OBB_COLOR_OFFSET,
    OBB_COUNT_STATUS_OFFSET,
    OBB_COORD_SCALE,
    OBB_INDEX_OFFSET,
    OBB_LINE_INDICES,
    OBB_POINT_RECORD_SIZE,
    OBB_SCALE_OFFSET,
    OBJECT_OBB_COLOR,
    STATUS_OFFSET,
    TERRAIN_RECORD_OFFSET,
    VOLUME_SELECTION_ALL,
    build_code_block,
    classify_climb_surfaces,
    build_obb_debug_data,
    build_obb_debug_records,
    extend_data_for_object_obbs,
    update_obb_debug_records,
)


def test_decode_live_type1_obb_layout_and_distance():
    raw = bytearray(0x100)
    offset = 0x10
    struct.pack_into("<BBH", raw, offset + 0x30, 1, 0xFF, 0x000B)
    struct.pack_into("<I", raw, offset + 0x34, OBB_VTABLE)
    struct.pack_into("<3f", raw, offset + 0x40, 1.0, 2.0, 3.0)
    for component_offset, axis in zip(
        (0x50, 0x60, 0x70),
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    ):
        struct.pack_into("<4f", raw, offset + component_offset, *axis, 0.0)
    struct.pack_into("<4f", raw, offset + 0x80, 10.0, 20.0, 30.0, 1.0)

    shape = PhysicsShapeSampler._decode_obb(
        bytes(raw), offset, 0x00E00000, (14.0, 20.0, 30.0)
    )

    assert shape is not None
    assert shape.owner == 0xFF
    assert shape.flags == 0x000B
    assert shape.half_extents == (1.0, 2.0, 3.0)
    assert shape.center == (10.0, 20.0, 30.0)
    assert shape.distance == 3.0


def test_native_fence_material_is_a_static_climb_candidate_without_bit_80():
    vertices = [
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 2.0),
        (2.0, 0.0, 0.0),
    ]
    triangles = [(0, 1, 2, 0x0001, MATERIAL_CLIMB_FENCE, 0)]

    assert classify_climb_surfaces(vertices, triangles) == {0: "standard"}


def test_unused_type1_pool_entry_is_rejected():
    raw = bytearray(0xA0)
    struct.pack_into("<BBH", raw, 0x30, 1, 0xFF, 0x0001)
    struct.pack_into("<I", raw, 0x34, OBB_VTABLE)
    assert PhysicsShapeSampler._decode_obb(
        bytes(raw), 0, 0x00E00000, (0.0, 0.0, 0.0)
    ) is None


def test_obb_wireframe_fits_shared_native_line_batch():
    shape = PhysicsObb(
        address=0x00E00000,
        owner=0xFF,
        flags=0x000B,
        half_extents=(1.0, 2.0, 3.0),
        axes=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        center=(10.0, 20.0, 30.0),
        distance=0.0,
    )

    points, lines, count = build_obb_debug_data(shape)

    assert len(points) == 8 * 12
    assert len(lines) == 12 * 8
    assert count == 12
    assert struct.unpack_from("<3f", points) == (9.0, 27.0, -18.0)


def test_multiple_obb_records_and_extended_layout_fit_capacity():
    shape = PhysicsObb(
        address=0x00E00000,
        owner=0xFF,
        flags=0x000B,
        half_extents=(1.0, 2.0, 3.0),
        axes=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        center=(10.0, 20.0, 30.0),
        distance=0.0,
    )

    records, count = build_obb_debug_records([shape, shape])
    extended = extend_data_for_object_obbs(bytes(0x1000))

    assert count == 2
    assert len(records) == 2 * 8 * 6
    assert struct.unpack_from("<3h", records) == (288, 864, -576)
    assert len(extended) == EXTENDED_DATA_SIZE
    assert extended[
        OBB_INDEX_OFFSET : OBB_INDEX_OFFSET + len(OBB_LINE_INDICES)
    ] == OBB_LINE_INDICES
    assert extended[OBB_COLOR_OFFSET : OBB_COLOR_OFFSET + 4] == bytes(
        OBJECT_OBB_COLOR
    )
    assert struct.unpack_from("<f", extended, OBB_SCALE_OFFSET)[0] == (
        OBB_COORD_SCALE
    )
    assert OBB_CAPACITY == 24


def test_obb_update_can_publish_dynamic_climb_color_before_count():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def write(self, address, value, bits):
            self.calls.append(("write", address, value, bits))

        def write_block(self, address, value):
            self.calls.append(("write_block", address, value))

    client = FakeClient()
    code_address = 0x00800000
    data_address = 0x00900000
    records = bytes(OBB_POINT_RECORD_SIZE)

    visible = update_obb_debug_records(
        client,
        code_address,
        data_address,
        records,
        1,
        enabled=True,
        color=CLIMB_DYNAMIC_OBB_COLOR,
    )

    count_address = code_address + STATUS_OFFSET + OBB_COUNT_STATUS_OFFSET
    assert visible == 1
    assert client.calls[0] == ("write", count_address, 0, 32)
    assert client.calls[1] == (
        "write",
        data_address + OBB_COLOR_OFFSET,
        int.from_bytes(bytes(CLIMB_DYNAMIC_OBB_COLOR), "little"),
        32,
    )
    assert client.calls[-1] == ("write", count_address, 1, 32)


def test_nearby_obb_sampler_sorts_and_limits_results():
    page = 0x00E00000
    raw = bytearray(PAGE_SIZE)

    def write_obb(offset: int, center_x: float) -> int:
        struct.pack_into("<BBH", raw, offset + 0x30, 1, 0xFF, 0x000B)
        struct.pack_into("<I", raw, offset + 0x34, OBB_VTABLE)
        struct.pack_into("<3f", raw, offset + 0x40, 1.0, 1.0, 1.0)
        for component_offset, axis in zip(
            (0x50, 0x60, 0x70),
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ):
            struct.pack_into("<4f", raw, offset + component_offset, *axis, 0.0)
        struct.pack_into("<4f", raw, offset + 0x80, center_x, 0.0, 0.0, 1.0)
        return page + offset

    farther = write_obb(0x100, 8.0)
    nearer = write_obb(0x200, 3.0)

    class FakeClient:
        def read_block(self, address, size, *, progress=False):
            assert address == page
            assert size == PAGE_SIZE
            return bytes(raw)

    sampler = PhysicsShapeSampler.__new__(PhysicsShapeSampler)
    sampler.client = FakeClient()
    sampler.obb_objects_by_page = {page: [farther, nearer]}
    sampler.sample_reads = 0

    rows = sampler.sample_nearby_obbs(
        (0.0, 0.0, 0.0), maximum_distance=20.0, limit=1
    )

    assert [row.address for row in rows] == [nearer]
    assert sampler.sample_reads == 1


def test_climb_obb_sampler_keeps_car_bounds_and_excludes_raw_props():
    page = 0x00E00000
    car_object = 0x00F14B10
    raw = bytearray(PAGE_SIZE)

    def write_obb(offset: int, center_x: float) -> int:
        struct.pack_into("<BBH", raw, offset + 0x30, 1, 0xFF, 0x000B)
        struct.pack_into("<I", raw, offset + 0x34, OBB_VTABLE)
        struct.pack_into("<3f", raw, offset + 0x40, 1.0, 1.0, 1.0)
        for component_offset, axis in zip(
            (0x50, 0x60, 0x70),
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ):
            struct.pack_into("<4f", raw, offset + component_offset, *axis, 0.0)
        struct.pack_into("<4f", raw, offset + 0x80, center_x, 0.0, 0.0, 1.0)
        return page + offset

    car_body = write_obb(0x100, 4.0)
    car_cabin = write_obb(0x200, 5.0)
    generic_prop = write_obb(0x300, 2.0)
    words = {
        car_object: CAR_VTABLE,
        car_object + CAR_OBB_POINTER_OFFSETS[0]: car_body,
        car_object + CAR_OBB_POINTER_OFFSETS[1]: car_cabin,
    }

    class FakeClient:
        def read_blocks(self, ranges):
            blocks = []
            for address, size in ranges:
                block = bytearray(size)
                for word_address, value in words.items():
                    if address <= word_address <= address + size - 4:
                        struct.pack_into(
                            "<I", block, word_address - address, value
                        )
                blocks.append(bytes(block))
            return blocks

        def read_block(self, address, size, *, progress=False):
            assert address == page
            assert size == PAGE_SIZE
            return bytes(raw)

    sampler = PhysicsShapeSampler.__new__(PhysicsShapeSampler)
    sampler.client = FakeClient()
    sampler.obb_objects_by_page = {
        page: [car_body, car_cabin, generic_prop]
    }
    sampler.obb_addresses = {car_body, car_cabin, generic_prop}
    sampler.climb_objects = {car_object: CAR_VTABLE}
    sampler.sample_reads = 0

    climb_rows = sampler.sample_nearby_climb_obbs(
        (0.0, 0.0, 0.0), maximum_distance=20.0, limit=24
    )
    raw_rows = sampler.sample_nearby_obbs(
        (0.0, 0.0, 0.0), maximum_distance=20.0, limit=24
    )

    assert [row.address for row in climb_rows] == [car_body, car_cabin]
    assert [row.address for row in raw_rows] == [
        generic_prop,
        car_body,
        car_cabin,
    ]


def test_climb_obb_sampler_rejects_a_reused_non_car_object_slot():
    object_address = 0x00F14B10

    class FakeClient:
        def read_blocks(self, ranges):
            return [bytes(size) for _address, size in ranges]

    sampler = PhysicsShapeSampler.__new__(PhysicsShapeSampler)
    sampler.client = FakeClient()
    sampler.obb_objects_by_page = {}
    sampler.obb_addresses = {0x00E00100}
    sampler.climb_objects = {object_address: CAR_VTABLE}
    sampler.sample_reads = 0

    assert sampler.sample_nearby_climb_obbs((0.0, 0.0, 0.0)) == []


def test_xray_renderer_preserves_code_and_terrain_capacity_boundary():
    common = (
        0x00800000,
        0x00900000,
        0x00900100,
        0x00900200,
        0x00900300,
        0x00900400,
        0x00900500,
        1,
        0x00900600,
        0x00900700,
        0x00900720,
        0x00800FE5,
        0,
    )
    normal_blob, normal_size = build_code_block(*common)
    xray_blob, xray_size = build_code_block(*common, collision_xray=True)
    extended_blob, extended_size = build_code_block(
        *common,
        collision_xray=True,
        object_obb_support=True,
    )

    assert normal_size == xray_size
    assert normal_size <= TERRAIN_RECORD_OFFSET
    assert normal_blob != xray_blob
    assert xray_size < extended_size <= DESTINATION_OFFSET
    assert len(extended_blob) == len(normal_blob) == 0x1000

    magic, trigger_count, current_count, selection = (
        FLAG_RENDER_HEADER.unpack_from(normal_blob, FLAG_RENDER_HEADER_OFFSET)
    )
    assert magic == FLAG_RENDER_MAGIC
    assert trigger_count == current_count == 0
    assert selection == VOLUME_SELECTION_ALL
