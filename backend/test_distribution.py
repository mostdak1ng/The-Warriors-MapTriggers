import hashlib
import json
from pathlib import Path
import struct
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'research'))
import controller_input as c
import prepare

BASE = 0x800000
CAVE = BASE + c.ENTRY_OFFSET
CAPTURE = BASE + c.CAPTURE_OFFSET
TTL = BASE + c.TTL_OFFSET
MARKER = BASE + c.MARKER_OFFSET


class Memory:
    def __init__(self):
        self.data = bytearray(0x2000000)
        self.writes = []
        self.write(c.HOOK, c.ORIGINAL, 32)
        self.writes.clear()

    def read(self, address, bits):
        return int.from_bytes(self.data[address:address+bits//8], 'little')

    def read_block(self, address, size, **kwargs):
        return bytes(self.data[address:address+size])

    def write(self, address, value, bits):
        self.write_block(address, value.to_bytes(bits//8, 'little'))

    def write_block(self, address, value):
        self.writes.append((address, bytes(value)))
        self.data[address:address+len(value)] = value


def run_guest(mem, physical, entry=CAVE):
    """Execute the emitted pad routine with a native-refresh stub, delay slots included."""
    regs = [0] * 32
    regs[29], regs[31] = 0x1FF0000, 0xFFFF0000
    pc, pending = entry, None
    refresh_input = None
    for _ in range(100):
        if pc == 0xFFFF0000:
            return refresh_input
        if pc == 0x149770:
            refresh_input = mem.read_block(0x5DE4D0, 8)
            mem.write_block(0x5DE4D0, physical)
            pc = regs[31]
            continue
        word = mem.read(pc, 32)
        op, rs, rt, imm = word >> 26, (word >> 21) & 31, (word >> 16) & 31, word & 65535
        signed = imm - 65536 if imm & 32768 else imm
        address = (regs[rs] + signed) & 0xFFFFFFFF
        target = None
        if word == 0: pass
        elif op == 9: regs[rt] = address
        elif op == 15: regs[rt] = imm << 16
        elif op == 13: regs[rt] = regs[rs] | imm
        elif op in (35, 55): regs[rt] = mem.read(address, 32 if op == 35 else 64)
        elif op in (41, 43, 63): mem.write(address, regs[rt], {41:16, 43:32, 63:64}[op])
        elif op == 4: target = pc + 4 + signed * 4 if regs[rs] == regs[rt] else pc + 8
        elif op == 2: target = (word & 0x3FFFFFF) << 2
        elif op == 3: regs[31] = pc + 8; target = (word & 0x3FFFFFF) << 2
        elif op == 0 and word & 63 == 8: target = regs[rs]
        else: raise AssertionError(hex(word))
        pc = pending if pending is not None else pc + 4
        pending = target
        regs[0] = 0
    raise AssertionError('guest did not return')


def test_guest_capture_neutralization_history_and_timeout():
    mem = Memory(); pad = c.PadCapture(mem,BASE); pad.install()
    previous = struct.pack('<4BHH', 1, 2, 3, 4, 0x1234, 0xFFFF)
    mem.write_block(CAPTURE, previous)
    mem.write(TTL, 2, 32)
    for step in range(3):
        physical = struct.pack('<4BHH', 255-step, 127, 17, 0, c.SELECT | c.START, 0)
        assert run_guest(mem, physical) == previous
        assert mem.read_block(CAPTURE, 8) == physical
        assert mem.read(TTL, 32) == max(0, 1-step)
        assert mem.read_block(0x5DE4D0, 8) == (b'\x7f'*4+b'\0'*4 if step < 2 else physical)
        previous = physical
    assert mem.read(MARKER, 32) == c.MARKER_VALUE


@pytest.mark.parametrize('address', [c.HOOK, CAVE, BASE+c.TAIL_OFFSET+0x28, MARKER])
def test_install_conflicts_are_readonly(address):
    mem = Memory(); mem.write(address, 0x12345678, 32); mem.writes.clear()
    with pytest.raises(RuntimeError): c.PadCapture(mem,BASE).install()
    assert not mem.writes


@pytest.mark.parametrize('address', [c.HOOK, CAVE, BASE+c.TAIL_OFFSET+0x28, MARKER])
def test_remove_does_not_overwrite_a_lost_owner(address):
    mem = Memory(); pad = c.PadCapture(mem,BASE); pad.install()
    mem.write(address, 0x12345678, 32); mem.writes.clear()
    with pytest.raises(RuntimeError): pad.close()
    assert not mem.writes


def test_reinstall_preserves_inert_executable_words():
    mem = Memory(); first = c.PadCapture(mem,BASE); first.install(); first.close()
    assert mem.read(c.HOOK, 32) == c.ORIGINAL
    assert mem.read(TTL, 32) == 0
    mem.writes.clear(); second = c.PadCapture(mem,BASE); second.install()
    assert all(address not in range(CAVE, CAPTURE) for address, _ in mem.writes)
    second.close()


@pytest.mark.parametrize('buttons,key', [(c.SELECT|c.R3, 'F8'), (c.SELECT|c.CIRCLE, 'G'), (c.SELECT|c.SQUARE, 'P')])
def test_controller_chords_are_edges(buttons, key):
    mem = Memory(); adapter = c.ControllerInput(mem,BASE); adapter.controller_mode = True
    adapter.process(127,127,127,127,buttons,0,True,True)
    assert adapter.pulses == {key}
    adapter.pulses.clear(); adapter.process(127,127,127,127,buttons,0.01,True,True)
    assert not adapter.pulses


def test_exit_requires_hold_then_full_release():
    adapter = c.ControllerInput(Memory(),BASE); adapter.controller_mode = True
    for now, buttons, armed, exit_requested in [(0,c.SELECT|c.START,False,False),
            (1.49,c.SELECT|c.START,False,False), (1.5,c.SELECT|c.START,True,False),
            (1.6,c.START,True,False), (1.7,0,True,True)]:
        adapter.process(127,127,127,127,buttons,now,True,True)
        assert adapter.exit_armed == armed and adapter.exit_requested == exit_requested


def test_focus_loss_and_transition_release_pad_and_motion():
    mem = Memory(); adapter = c.ControllerInput(mem,BASE); adapter.start(); adapter.controller_mode = True
    mem.write_block(CAPTURE, struct.pack('<4BHH',255,0,255,255,c.R1,0))
    adapter.sample(0,True,True); assert mem.read(TTL,32) == 60 and any(adapter.move)
    adapter.sample(.1,True,False); assert mem.read(TTL,32) == 0 and not any(adapter.move)
    adapter.sample(.2,True,True,True); assert mem.read(TTL,32) == 0 and not any(adapter.look)


def test_keyboard_handoff_retains_freecam_and_releases_controller():
    adapter = c.ControllerInput(Memory(),BASE); adapter.controller_mode = True
    adapter.process(127,127,127,127,0,0,True,True,keyboard_v=True)
    assert adapter.requested_enabled is True and not adapter.controller_mode


def test_analog_speed_is_proportional_and_select_prevents_motion():
    adapter = c.ControllerInput(Memory(),BASE); adapter.controller_mode = True
    adapter.process(190,127,127,127,c.L2,0,True,True)
    assert 0 < adapter.move[0] < 1 and adapter.speed_factor == 4
    adapter.move = (0,0,0); adapter.process(255,0,255,0,c.SELECT,0,True,True)
    assert not any(adapter.move)


def test_deadzone_and_diagonal_clamping():
    assert c.stick(127,128) == (0,0)
    x,y = c.stick(255,255)
    assert x*x+y*y == pytest.approx(1)


def test_invalid_image_rejected_without_cache(tmp_path):
    iso = tmp_path/'wrong.iso'; iso.write_bytes(b'not a disc')
    with pytest.raises(ValueError): prepare.prepare(iso,tmp_path/'cache')
    assert not (tmp_path/'cache').exists()


def test_cache_requires_every_dataset_and_rejects_tamper(tmp_path):
    (tmp_path/'volumes').mkdir()
    catalog = dict(entries=[dict(dataset='volumes/a.json')])
    files = {'lua_volume_catalog.json':json.dumps(catalog).encode(), 'volumes/a.json':b'{}'}
    for name, data in files.items(): (tmp_path/name).write_bytes(data)
    manifest = dict(version=prepare.CACHE_VERSION,image_sha256='image',files={
        name:hashlib.sha256(data).hexdigest() for name,data in files.items()})
    (tmp_path/'cache.json').write_text(json.dumps(manifest))
    assert prepare.valid_cache(tmp_path,'image')
    (tmp_path/'volumes/a.json').write_text('tamper')
    assert not prepare.valid_cache(tmp_path,'image')
    manifest['files'].pop('volumes/a.json'); (tmp_path/'cache.json').write_text(json.dumps(manifest))
    assert not prepare.valid_cache(tmp_path,'image')


def test_cache_manifest_cannot_reference_a_parent(tmp_path):
    root = tmp_path/'cache'; root.mkdir(); outside = tmp_path/'secret'; outside.write_bytes(b'x')
    manifest = dict(version=prepare.CACHE_VERSION,image_sha256='x',files={'../secret':hashlib.sha256(b'x').hexdigest()})
    (root/'cache.json').write_text(json.dumps(manifest))
    assert not prepare.valid_cache(root,'x')


@pytest.mark.parametrize('base', [0x800000,0x80F810,0x1FED810])
@pytest.mark.parametrize('ttl', [0,1,60])
def test_real_relocated_guest_addresses_and_full_lease(base, ttl):
    mem=Memory(); pad=c.PadCapture(mem,base); pad.install()
    mem.write(pad.ttl,ttl,32)
    for step in range(ttl+1):
        physical=struct.pack('<4BHH',12,244,17,199,c.R1,0)
        run_guest(mem,physical,pad.entry)
        assert mem.read_block(pad.capture,8)==physical
        assert mem.read(pad.ttl,32)==max(0,ttl-step-1)
        assert mem.read_block(0x5DE4D0,8)==(b'\x7f'*4+b'\0'*4 if step<ttl else physical)
    assert not any(mem.read_block(0x50A810,0x70))
    pad.close()


def test_transport_error_after_hook_commit_remains_removable():
    class TransportFailure(Memory):
        fail=False
        def write(self,address,value,bits):
            super().write(address,value,bits)
            if self.fail and address==c.HOOK and value!=c.ORIGINAL:
                self.fail=False
                raise OSError('reply lost after commit')
    mem=TransportFailure(); pad=c.PadCapture(mem,BASE); mem.fail=True
    with pytest.raises(OSError): pad.install()
    assert pad.installed
    c.disarm(mem,BASE)
    assert mem.read(c.HOOK,32)==c.ORIGINAL and mem.read(pad.ttl,32)==0


def test_renderer_has_nonoverlapping_input_reservations():
    from simultaneous_debug_renderer import build_code_block
    common=(BASE,0x900000,0x900100,0x900200,0x900300,0x900400,0x900500,
            1,0x900600,0x900700,0x900720,BASE+0xFE5,0)
    for xray in (False,True):
        blob,size=build_code_block(*common,collision_xray=xray,object_obb_support=True)
        assert size<=c.ENTRY_OFFSET
        assert not any(blob[c.ENTRY_OFFSET:c.ENTRY_OFFSET+0x40])
        assert not any(blob[c.TAIL_OFFSET:c.TAIL_OFFSET+0x40])
        original=bytearray(blob)
        first,tail=c.build_blobs(BASE)
        original[c.ENTRY_OFFSET:c.ENTRY_OFFSET+0x40]=first
        original[c.TAIL_OFFSET:c.TAIL_OFFSET+0x40]=tail
        assert original[0x680:0x7C0]==blob[0x680:0x7C0]
        assert original[0x800:]==blob[0x800:]


def test_cold_cache_warm_reuse_and_corruption_rebuild(tmp_path,monkeypatch):
    image=tmp_path/'source.iso';image.write_bytes(b'private test source')
    identity='a'*64
    monkeypatch.setattr(prepare,'image_identity',lambda _:identity)
    calls=[]
    def scan(_):
        calls.append(True)
        return (dict(parse_errors=[],entries=[dict(entry=1,id='test',level_id='test',
                display_name='Synthetic',dataset='volumes/test.json')]),{1:[]},{1:[]})
    monkeypatch.setattr(prepare,'scan',scan)
    first=prepare.prepare(image,tmp_path/'cache')
    second=prepare.prepare(image,tmp_path/'cache')
    assert first==second and len(calls)==1
    target=Path(first['catalog']).parent
    (target/'volumes/test.json').write_text('corrupt')
    assert prepare.prepare(image,tmp_path/'cache')==first and len(calls)==2
    assert prepare.valid_cache(target,identity)
    assert not list((tmp_path/'cache').glob('building-*'))
