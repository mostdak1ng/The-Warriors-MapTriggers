"""Instruction interleavings and host-side retirement of allocator shims."""
import struct
from unittest.mock import patch

import pytest
import ingame_volume_probe as heap


class Machine:
    def __init__(self, code, stalled=False):
        self.memory = {heap.BOOTSTRAP_ADDRESS+i: b for i, b in enumerate(code)}
        self.reg = [0]*32
        self.reg[29], self.reg[31] = 0x10000, 0x1567CC
        self.pc, self.pending = heap.BOOTSTRAP_ADDRESS, None
        self.calls, self.stalled = [], stalled
        self.frame = 0

    def read(self, address, bits=32):
        return sum(self.memory.get(address+i, 0) << (8*i) for i in range(bits//8))

    def write(self, address, value, bits=32):
        for i in range(bits//8):
            self.memory[address+i] = (value >> (8*i)) & 255

    def step(self):
        if self.pc == 0x1567CC:
            return False
        if self.pc in (heap.GAME_MALLOC, heap.GAME_FREE, heap.ORIGINAL_CALLEE):
            if self.stalled:
                return True
            self.calls.append((self.pc, self.reg[4]))
            if self.pc == heap.GAME_MALLOC:
                self.reg[2] = 0x1800000
            if self.pc == heap.ORIGINAL_CALLEE:
                self.frame += 1
            self.pc = self.reg[31]
            return True
        word = self.read(self.pc)
        op, rs, rt = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        imm = word & 65535
        signed = imm if imm < 32768 else imm-65536
        branch = None
        if word == 0:
            pass
        elif op == 9:
            self.reg[rt] = (self.reg[rs]+signed) & 0xFFFFFFFF
        elif op == 15:
            self.reg[rt] = imm << 16
        elif op == 35:
            self.reg[rt] = self.read((self.reg[rs]+signed) & 0xFFFFFFFF)
        elif op == 43:
            self.write((self.reg[rs]+signed) & 0xFFFFFFFF, self.reg[rt])
        elif op == 5:
            if self.reg[rs] != self.reg[rt]:
                branch = self.pc+4+signed*4
        elif op in (2, 3):
            branch = (word & 0x3FFFFFF) << 2
            if op == 3:
                self.reg[31] = self.pc+8
        else:
            raise AssertionError(f"Unexpected instruction {word:08X} at {self.pc:08X}")
        self.pc = self.pending if self.pending is not None else self.pc+4
        self.pending = branch
        return True


BUILDERS = [lambda: heap.build_heap_callsite_probe(),
            lambda: heap.build_heap_bootstrap(0x1000),
            lambda: heap.build_heap_bootstrap(0x1800000, freeing=True),
            lambda: heap.build_heap_free_pair_bootstrap(0x1800000, 0x1810000)]


@pytest.mark.parametrize('builder', BUILDERS)
@pytest.mark.parametrize('reentry', (False, True))
def test_detach_at_every_instruction_never_erases_a_return_path(builder, reentry):
    for detach in range(65):
        machine = Machine(builder())
        detached = False
        erased = False
        for tick in range(180):
            if tick == detach:
                detached = True
                machine.write(heap.BOOTSTRAP_DONE, 0)
                start_frame = machine.frame
            if detached and not erased and (machine.read(heap.BOOTSTRAP_DONE) or machine.frame-start_frame >= 2):
                assert not (heap.BOOTSTRAP_ADDRESS <= machine.pc < heap.BOOTSTRAP_RESULT)
                # No callee can return into the gap after the tail jump's slot.
                assert not (heap.BOOTSTRAP_ADDRESS <= machine.reg[31] < heap.BOOTSTRAP_RESULT)
                for i in range(heap.BOOTSTRAP_SIZE):
                    machine.memory[heap.BOOTSTRAP_ADDRESS+i] = 0
                erased = True
            running = machine.step()
            if not running:
                if not detached and reentry:
                    machine.pc = heap.BOOTSTRAP_ADDRESS
                elif detached:
                    assert machine.reg[29] == 0x10000
                    assert machine.reg[31] == 0x1567CC
                    break
        else:
            raise AssertionError('Shim failed to return')
        allocs = [c for c in machine.calls if c[0] == heap.GAME_MALLOC]
        frees = [c for c in machine.calls if c[0] == heap.GAME_FREE]
        assert len(allocs) <= 1
        assert len(frees) <= 2
        assert len({p for _, p in frees}) == len(frees)


def test_old_timeout_erasure_destroys_an_allocator_return_path():
    machine = Machine(heap.build_heap_bootstrap(0x1000))
    while machine.pc != heap.GAME_MALLOC:
        machine.step()
    assert heap.BOOTSTRAP_ADDRESS <= machine.reg[31] < heap.BOOTSTRAP_RESULT
    for i in range(heap.BOOTSTRAP_SIZE):
        machine.memory[heap.BOOTSTRAP_ADDRESS+i] = 0
    machine.step()
    assert machine.read(machine.pc) == 0
    assert machine.reg[29] != 0x10000


class Client:
    def __init__(self, paused=False, never_enter=False, stalled=False):
        self.machine = Machine(bytes(heap.BOOTSTRAP_SIZE), stalled)
        self.machine.pc = 0x1567CC
        self.machine.write(heap.BOOTSTRAP_HOOK_ADDRESS, heap.BOOTSTRAP_HOOK_ORIGINAL)
        self.paused, self.never_enter = paused, never_enter
        self.cleared = False

    def read(self, address, bits):
        return self.machine.frame if address == heap.BOOTSTRAP_FRAME else self.machine.read(address, bits)

    def write(self, address, value, bits):
        self.machine.write(address, value, bits)

    def read_block(self, address, size, **kwargs):
        return bytes(self.machine.memory.get(address+i, 0) for i in range(size))

    def write_block(self, address, data):
        if not any(data):
            assert not (heap.BOOTSTRAP_ADDRESS <= self.machine.pc < heap.BOOTSTRAP_RESULT)
            assert not (heap.BOOTSTRAP_ADDRESS <= self.machine.reg[31] < heap.BOOTSTRAP_RESULT)
            self.cleared = True
        for i, value in enumerate(data):
            self.machine.memory[address+i] = value

    def advance(self, seconds):
        if self.paused:
            return
        if self.never_enter:
            self.machine.frame += 1
            return
        if self.machine.pc == 0x1567CC:
            if self.machine.read(heap.BOOTSTRAP_HOOK_ADDRESS) == heap.jal(heap.BOOTSTRAP_ADDRESS):
                self.machine.pc = heap.BOOTSTRAP_ADDRESS
            else:
                self.machine.frame += 1
                return
        self.machine.step()


def invoke(client, function, cancel=lambda: False):
    ticks = [0.]
    def sleep(seconds):
        ticks[0] += seconds
        client.advance(seconds)
    with patch.object(heap.time, 'monotonic', side_effect=lambda: ticks[0]), \
         patch.object(heap.time, 'sleep', side_effect=sleep), \
         patch.object(heap, 'bootstrap_cancelled', side_effect=cancel):
        return function()


def test_completed_malloc_retirement_clears_only_after_safe_return():
    client = Client()
    assert invoke(client, lambda: heap.run_heap_bootstrap(client, 0x1000)) == 0x1800000
    assert client.cleared


def test_paused_before_entry_can_resume_and_complete_without_free_retry():
    client = Client(paused=True)
    polls = [0]
    def resume():
        polls[0] += 1
        if polls[0] == 500:
            client.paused = False
        return False
    # Hook was detached on timeout; after resume there was no entry, so the
    # caller can submit a fresh operation without risking duplicate frees.
    with pytest.raises(heap.BootstrapNotEntered):
        invoke(client, lambda: heap.run_heap_free_pair_bootstrap(client, 0x1800000, 0x1810000), resume)
    assert client.cleared
    assert not client.machine.calls


def test_second_stop_retains_executable_scratch_without_heap_release():
    client = Client(paused=True)
    with pytest.raises(heap.BootstrapCancelled):
        invoke(client, lambda: heap.probe_heap_callsite(client), lambda: True)
    assert not client.cleared
    assert not client.machine.calls
    assert client.read(heap.BOOTSTRAP_HOOK_ADDRESS, 32) == heap.BOOTSTRAP_HOOK_ORIGINAL


def test_entered_free_timeout_is_uncertain_and_never_erases_code():
    client = Client(stalled=True)
    with pytest.raises(heap.BootstrapUncertain):
        invoke(client, lambda: heap.run_heap_free_pair_bootstrap(client, 0x1800000, 0x1810000))
    assert not client.cleared
    assert client.read(heap.BOOTSTRAP_ENTRY, 32) == 1
    assert client.read(heap.BOOTSTRAP_HOOK_ADDRESS, 32) == heap.BOOTSTRAP_HOOK_ORIGINAL
