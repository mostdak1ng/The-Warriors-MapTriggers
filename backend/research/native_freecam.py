"""Experimental render-synchronous freecam, independent of the 30/60 Hz scheduler.

The host publishes a matrix only on change. The EE copies a complete publication
to its own last-good matrix before the original DevRWView transform setter runs.
An interrupted publication retains the last-good camera, never the game camera.
All storage belongs to the combined renderer's existing heap allocation.
"""
import math
import struct

from ingame_volume_probe import REG, addiu, beq, bne, load_address, lw, sw
from pine_client import GAME_ID, UUID

HOOK = 0x1989B0
ORIGINAL = (0x27BDFFB0, 0x00A0102D)
CONTEXT = (0xFFBF0040, 0x24030003, 0x03A0282D, 0x0000302D, 0xC4440034)
STUB = 0xEB0
STUB_END = 0xF80
STATE = 0x720
STAGING = 0x740
STABLE = 0x780
ALLOCATION_SIZE = 0x1000
# STATE: sequence, consumed sequence, enabled, world, applied calls, reserved.
WORLD = 0x50CCE4
DEVICE = 0x50CDB4


def jump(address):
    return 0x08000000 | (address >> 2)


def hook_words(base):
    return jump(base + STUB), ORIGINAL[0]


def build_stub(base):
    t0, t1, t2, t3, t4 = 8, 9, 10, 11, 12
    words = []
    branches = []
    labels = {}

    def branch(op, a, b, label, delay=0):
        branches.append((len(words), op, a, b, label))
        words.extend((0, delay))

    load_address(words, t0, base + STATE)
    words.append(lw(t1, 8, t0))
    branch(beq, t1, 0, 'native')
    load_address(words, t1, WORLD)
    words.extend((lw(t1, 0, t1), lw(t2, 12, t0)))
    branch(bne, t1, t2, 'native')
    load_address(words, t1, DEVICE)
    words.append(lw(t1, 0, t1))
    branch(beq, t1, 0, 'native')
    words.append(addiu(t1, t1, 0x70))
    branch(bne, REG['a0'], t1, 'native')
    words.extend((lw(t1, 0, t0), lw(t2, 4, t0)))
    branch(beq, t1, t2, 'apply')
    words.append(0x30000000 | (t1 << 21) | (t2 << 16) | 1)  # andi t2,t1,1
    branch(bne, t2, 0, 'apply')
    # Snapshot all four quadwords in registers; recheck publication sequence
    # before touching EE-owned stable storage. Host writes never target STABLE.
    for reg, offset in zip((t2, t3, t4, 13), range(0, 64, 16)):
        words.append((0x1E << 26) | (t0 << 21) | (reg << 16) | (STAGING-STATE+offset))
    words.append(lw(14, 0, t0))
    branch(bne, t1, 14, 'apply')
    for reg, offset in zip((t2, t3, t4, 13), range(0, 64, 16)):
        words.append((0x1F << 26) | (t0 << 21) | (reg << 16) | (STABLE-STATE+offset))
    words.append(sw(t1, 4, t0))
    labels['apply'] = len(words)
    # First publication must have been consumed. This also makes a torn initial
    # publication fail closed while enabled remains zero on the host.
    words.append(lw(t1, 4, t0))
    branch(beq, t1, 0, 'native')
    words.extend((addiu(REG['a1'], t0, STABLE-STATE), lw(t1, 16, t0),
                  addiu(t1, t1, 1), sw(t1, 16, t0)))
    labels['native'] = len(words)
    words.extend((jump(HOOK+8), ORIGINAL[1]))
    for idx, op, a, b, label in branches:
        words[idx] = op(a, b, labels[label]-(idx+1))
    result = struct.pack(f'<{len(words)}I', *words)
    assert len(result) <= STUB_END-STUB
    return result


def extend_blob(blob, base):
    assert len(blob) == ALLOCATION_SIZE
    result = bytearray(blob)
    assert not any(result[STATE:STABLE+64]), "Native state overlaps another renderer feature"
    result[STUB:STUB_END] = bytes(STUB_END-STUB)
    stub = build_stub(base)
    from map_visibility_control import RANGE_STUB_OFFSET, extend_range_blob
    if STUB+len(stub) > RANGE_STUB_OFFSET:
        raise ValueError('Native camera code overlaps immutable range entries')
    result[STUB:STUB+len(stub)] = stub
    return extend_range_blob(result)


def read_words(client, address, count):
    return tuple(client.read(address+4*i, 32) for i in range(count))


def disarm(client, base):
    """Idempotent and ownership checked; safe before any renderer heap release."""
    current = read_words(client, HOOK, 2)
    if current == ORIGINAL:
        return
    if current != hook_words(base):
        raise RuntimeError('Native freecam hook belongs to another owner; not restored')
    client.write(HOOK, ORIGINAL[0] | (ORIGINAL[1] << 32), 64)
    if read_words(client, HOOK, 2) != ORIGINAL:
        raise RuntimeError('Native freecam restoration did not verify')


def matrix(position, quaternion):
    from freecam_probe import rotate
    if not all(math.isfinite(v) for v in (*position, *quaternion)):
        raise ValueError('Non-finite freecam transform')
    values = []
    for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
        values.extend((*rotate(quaternion, axis), 0.0))
    values.extend((*position, 1.0))
    return struct.pack('<16f', *values)


class NativeFreecam:
    def __init__(self, client, base):
        self.client, self.base = client, base
        self.owned = False
        self.sequence = 0
        self.enabled = False
        self.last = None
        self.publications = 0

    def install(self):
        p, base = self.client, self.base
        if p.text(GAME_ID) != 'SLUS-21215' or p.text(UUID).lower() != 'b99a75de':
            raise RuntimeError('Native freecam requires The Warriors USA B99A75DE')
        if not 0x700000 <= base <= 0x1FFF000-ALLOCATION_SIZE or base % 16:
            raise RuntimeError('Invalid renderer allocation')
        if read_words(p, HOOK, 2) != ORIGINAL or read_words(p, HOOK+8, len(CONTEXT)) != CONTEXT:
            raise RuntimeError('Native camera setter is modified; freecam not installed')
        stub = build_stub(base)
        if read_words(p, base+STUB, len(stub)//4) != struct.unpack(f'<{len(stub)//4}I', stub):
            raise RuntimeError('Renderer lacks the native freecam extension')
        if any(p.read_block(base+STATE, STABLE+64-STATE, progress=False)):
            raise RuntimeError('Native freecam state is already occupied')
        world = p.read(WORLD, 32)
        if not 0x700000 <= world < 0x1FFF000:
            raise RuntimeError('No valid world for native freecam')
        p.write(base+STATE+12, world, 32)
        self.owned = True  # transport failure after commit still needs cleanup
        replacement = hook_words(base)
        p.write(HOOK, replacement[0] | (replacement[1] << 32), 64)
        if read_words(p, HOOK, 2) != replacement:
            raise RuntimeError('Native freecam installation did not verify')

    def update(self, position, quaternion, enabled=True):
        if not self.owned:
            raise RuntimeError('Native freecam is not installed')
        p, base = self.client, self.base
        if enabled:
            raw = matrix(position, quaternion)
            if raw != self.last:
                # Odd sequence makes the EE retain the previous complete pose.
                # Even sequence commits the entire matrix, including orientation.
                self.sequence = (self.sequence + 2) & 0xFFFFFFFE or 2
                p.write(base+STATE, self.sequence-1, 32)
                p.write_block(base+STAGING, raw)
                p.write(base+STATE, self.sequence, 32)
                self.last = raw
                self.publications += 1
        if bool(enabled) != self.enabled:
            p.write(base+STATE+8, int(bool(enabled)), 32)
            self.enabled = bool(enabled)

    def close(self):
        if self.owned:
            disarm(self.client, self.base)
            self.owned = False
