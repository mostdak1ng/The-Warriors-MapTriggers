"""Execute the camera wrapper across live range switches, with delay slots."""
import struct
from unittest.mock import patch

import pytest
import map_visibility_control as v
import native_freecam as native
import simultaneous_debug_renderer as renderer
from test_map_visibility_control import FakePine, BASE


def execute_switch(initial, target, switch_step, legacy=False):
    old = lambda factor: ((0x460C6300,)*(factor.bit_length()-1)
                          +(0x08120AD8,0x8C840004)+(0,)*(7-factor.bit_length()))
    wrapper = lambda factor: (old(factor) if legacy and factor!=1 else
                              v.FAR_ORIGINAL if legacy else v.far_variant(BASE,factor))
    memory = {v.FAR_ADDRESS+i*4:word for i,word in enumerate(wrapper(initial))}
    memory.update({BASE+v.RANGE_STUB_OFFSET+i*4:word for i,word in enumerate(v.RANGE_STUB)})
    stack = {0x10000:0xABCD}
    regs = dict(sp=0x10000,ra=0xDEADBEEF,a0=0x900000)
    pc,pending,f12,consumed = v.FAR_ADDRESS,None,94.,None
    for step in range(60):
        if step==switch_step:
            memory.update({v.FAR_ADDRESS+i*4:word for i,word in enumerate(wrapper(target))})
        if pc==0xDEADBEEF:
            return regs, consumed, stack
        if pc==0x482B60:
            consumed=f12
            assert regs['a0']==0x900040
            pc=regs['ra'];continue
        word=memory[pc];branch=None
        if word==0:pass
        elif word==0x27BDFFF0:regs['sp']-=16
        elif word==0xFFBF0000:stack[regs['sp']]=regs['ra']
        elif word==0xDFBF0000:regs['ra']=stack[regs['sp']]
        elif word==0x27BD0010:regs['sp']+=16
        elif word==0x8C840004:regs['a0']=0x900040
        elif word==0x460C6300:f12=struct.unpack('<f',struct.pack('<f',f12+f12))[0]
        elif word==0x03E00008:branch=regs['ra']
        elif word>>26 in (2,3):
            branch=(word&0x3FFFFFF)<<2
            if word>>26==3:regs['ra']=pc+8
        else:raise AssertionError(hex(word))
        pc=pending if pending is not None else pc+4
        pending=branch
    raise AssertionError('Wrapper did not return')


@pytest.mark.parametrize('initial',v.RANGE_FACTORS)
@pytest.mark.parametrize('target',v.RANGE_FACTORS)
def test_live_switch_at_each_instruction_preserves_native_stack(initial,target):
    for at in range(24):
        regs,consumed,stack=execute_switch(initial,target,at)
        assert regs['sp']==0x10000
        assert regs['ra']==0xDEADBEEF
        assert stack[0x10000]==0xABCD
        assert consumed in (94.*initial,94.*target)


def test_old_full_wrapper_switch_reproduces_stack_corruption():
    # Positive regression oracle: old range->stock and stock->range can retain
    # the wrong stack frame if the function was already entered.
    for initial,target,at in ((1,8,2),(8,1,1)):
        regs,_,stack=execute_switch(initial,target,at,legacy=True)
        assert regs['sp']!=0x10000 or stack[0x10000]!=0xABCD


def test_range_entries_do_not_change_other_renderer_storage():
    blob=native.extend_blob(bytes(0x1000),BASE)
    stub=native.build_stub(BASE)
    assert native.STUB+len(stub)==v.RANGE_STUB_OFFSET
    assert blob[native.STUB:native.STUB+len(stub)]==stub
    assert struct.unpack_from('<5I',blob,v.RANGE_STUB_OFFSET)==v.RANGE_STUB
    assert v.RANGE_STUB_END==renderer.MATRIX_OFFSET
    assert not any(blob[renderer.MATRIX_OFFSET:])


@pytest.mark.parametrize('factor',(2,4,8))
def test_supervisor_detaches_range_without_erasing_inflight_code(factor):
    client=FakePine()
    client.seed(v.FAR_ADDRESS,v.far_variant(BASE,factor))
    v.disarm_range(client,BASE)
    assert v.words(client,v.FAR_ADDRESS,8)==v.FAR_ORIGINAL
    assert v.words(client,BASE+v.RANGE_STUB_OFFSET,5)==v.RANGE_STUB
    assert client.writes==[(v.FAR_ADDRESS+8,v.FAR_ORIGINAL[2])]


@pytest.mark.parametrize('address',(v.FAR_ADDRESS,v.FAR_ADDRESS+8,BASE+v.RANGE_STUB_OFFSET))
def test_foreign_range_owner_is_never_overwritten(address):
    client=FakePine();client.seed(v.FAR_ADDRESS,v.far_variant(BASE,8))
    client.memory[address]^=1
    with pytest.raises(RuntimeError):v.disarm_range(client,BASE)
    assert not client.writes


@pytest.mark.parametrize('heartbeat',(False,True))
def test_native_retirement_precedes_heap_release(heartbeat):
    events=[]
    class Markers:
        def read_block(self,address,size,**kwargs):
            return renderer.MARKER if address==BASE+renderer.MARKER_OFFSET else renderer.DATA_MARKER
    with patch.object(renderer,'disarm',side_effect=lambda *a:events.append('disarm')), \
         patch.object(renderer,'probe_heap_callsite',side_effect=lambda *a,**kw:events.append('retire') or heartbeat), \
         patch.object(renderer,'run_heap_free_pair_bootstrap',side_effect=lambda *a:events.append('free')):
        if heartbeat:assert renderer.cleanup(Markers(),BASE,0x900000)
        else:
            with pytest.raises(TimeoutError):renderer.cleanup(Markers(),BASE,0x900000)
    assert events==(['disarm','retire','disarm','free'] if heartbeat else ['disarm','retire'])


def test_ownership_lost_during_retirement_prevents_free():
    class Markers:
        calls=0
        def read_block(self,address,size,**kwargs):
            self.calls+=1
            if self.calls>2:return bytes(16)
            return renderer.MARKER if address==BASE+renderer.MARKER_OFFSET else renderer.DATA_MARKER
    with patch.object(renderer,'disarm'),patch.object(renderer,'probe_heap_callsite',return_value=True), \
         patch.object(renderer,'run_heap_free_pair_bootstrap') as release:
        with pytest.raises(RuntimeError):renderer.cleanup(Markers(),BASE,0x900000)
        release.assert_not_called()
