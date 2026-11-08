import struct
import pytest
import native_freecam as n

BASE = 0x800000
VIEW = 0x900070


class Memory:
    def __init__(self):
        self.data = bytearray(0x2000000)
        self.writes = []
        self.data[BASE:BASE+n.ALLOCATION_SIZE] = n.extend_blob(bytes(4096), BASE)
        for i, value in enumerate((*n.ORIGINAL, *n.CONTEXT)):
            struct.pack_into('<I', self.data, n.HOOK+4*i, value)
        struct.pack_into('<I', self.data, n.WORLD, 0xA00000)
        struct.pack_into('<I', self.data, n.DEVICE, VIEW-0x70)

    def text(self, opcode):
        return 'SLUS-21215' if opcode == n.GAME_ID else 'B99A75DE'

    def read(self, address, bits):
        return int.from_bytes(self.data[address:address+bits//8], 'little')

    def read_block(self, address, size, **kwargs):
        return bytes(self.data[address:address+size])

    def write(self, address, value, bits):
        self.write_block(address, value.to_bytes(bits//8, 'little'))

    def write_block(self, address, data):
        self.writes.append((address, bytes(data)))
        self.data[address:address+len(data)] = data


def execute(mem, view=VIEW, interrupt=None):
    """Execute emitted R5900 entry+stub including branch/jump delay slots."""
    regs = [0x13570000000000000000+i for i in range(32)]
    regs[0], regs[4], regs[5], regs[29] = 0, view, 0xB00000, 0x1FF0000
    original = regs.copy()
    pc, pending, steps = n.HOOK, None, 0
    while pc != n.HOOK+8:
        if interrupt:
            interrupt(steps, mem)
        word = mem.read(pc, 32)
        op, rs, rt = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        imm = word & 65535
        signed = imm-65536 if imm & 32768 else imm
        addr = (regs[rs]+signed) & 0xFFFFFFFF
        target = None
        if word == 0:
            pass
        elif op == 15:
            regs[rt] = imm << 16
        elif op == 13:
            regs[rt] = regs[rs] | imm
        elif op == 12:
            regs[rt] = regs[rs] & imm
        elif op == 9:
            regs[rt] = addr
        elif op == 35:
            regs[rt] = mem.read(addr,32)
        elif op == 43:
            mem.write(addr,regs[rt]&0xFFFFFFFF,32)
        elif op == 30:
            regs[rt] = mem.read(addr,128)
        elif op == 31:
            mem.write(addr,regs[rt],128)
        elif op in (4,5):
            taken = (regs[rs] == regs[rt]) == (op == 4)
            target = pc+4+signed*4 if taken else pc+8
        elif op == 2:
            target = (word & 0x3FFFFFF) << 2
        elif op == 0 and word & 63 == 45:
            regs[(word >> 11) & 31] = (regs[rs]+regs[rt]) & ((1<<64)-1)
        else:
            raise AssertionError(hex(word))
        pc = pending if pending is not None else pc+4
        pending = target
        steps += 1
        assert steps < 100
    assert regs[29] == original[29]-0x50
    assert regs[31] == original[31]
    assert regs[16:24] == original[16:24]
    assert regs[4] == original[4]
    assert regs[2] == regs[5]
    return regs[5]


def active():
    mem = Memory()
    control = n.NativeFreecam(mem, BASE)
    control.install()
    control.update((1,2,3), (0,0,0,1))
    assert execute(mem) == BASE+n.STABLE
    return mem, control


def test_idle_no_writes_and_exact_code_reversal():
    mem, control = active()
    before = len(mem.writes)
    for _ in range(240):
        control.update((1,2,3), (0,0,0,1))
    assert len(mem.writes) == before
    assert control.publications == 1
    control.update((1,2,3), (0,0,0,1), False)
    assert execute(mem) == 0xB00000
    control.close()
    assert n.read_words(mem,n.HOOK,2) == n.ORIGINAL
    control.close()


@pytest.mark.parametrize('bad_view,changed_world', [(True,False),(False,True)])
def test_other_view_and_world_bypass(bad_view,changed_world):
    mem, _ = active()
    if changed_world:
        mem.write(n.WORLD,0xC00000,32)
    assert execute(mem, VIEW+0x70 if bad_view else VIEW) == 0xB00000


def test_incomplete_publication_retains_last_good_pose():
    mem, _ = active()
    old = mem.read_block(BASE+n.STABLE,64)
    mem.write(BASE+n.STATE,3,32)
    mem.write_block(BASE+n.STAGING,b'\x55'*32)
    assert execute(mem) == BASE+n.STABLE
    assert mem.read_block(BASE+n.STABLE,64) == old


@pytest.mark.parametrize('when', range(48))
def test_publication_interleavings_never_mix_pose(when):
    mem, _ = active()
    old = mem.read_block(BASE+n.STABLE,64)
    next_pose = n.matrix((11,22,33),(0.5,0.5,0.5,0.5))
    mem.write(BASE+n.STATE,4,32)
    mem.write_block(BASE+n.STAGING,next_pose)
    final_pose = n.matrix((-11,-22,-33),(0,0,1,0))
    def interrupt(step,p):
        if step == when:
            p.write(BASE+n.STATE,5,32)
            p.write_block(BASE+n.STAGING, final_pose)
            p.write(BASE+n.STATE,6,32)
    assert execute(mem,interrupt=interrupt) == BASE+n.STABLE
    assert mem.read_block(BASE+n.STABLE,64) in (old,next_pose,final_pose)


@pytest.mark.parametrize('offset', range(0,28,4))
def test_context_rejects_before_writes(offset):
    mem = Memory()
    mem.data[n.HOOK+offset] ^= 1
    with pytest.raises(RuntimeError):
        n.NativeFreecam(mem,BASE).install()
    assert not mem.writes


def test_foreign_hook_not_overwritten_on_cleanup():
    mem, control = active()
    mem.write(n.HOOK,0x12345678,32)
    before = len(mem.writes)
    with pytest.raises(RuntimeError):
        control.close()
    assert len(mem.writes) == before
