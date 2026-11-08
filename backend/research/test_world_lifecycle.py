from unittest.mock import Mock, patch

import pytest

import world_lifecycle as lifecycle


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_wait(*, worlds, heartbeats, hotkeys=None, **kwargs):
    clock = Clock()
    with (
        patch.object(lifecycle.time, "monotonic", clock.monotonic),
        patch.object(lifecycle.time, "sleep", clock.sleep),
        patch.object(lifecycle, "read_world_identity", worlds),
        patch.object(lifecycle, "probe_heap_callsite", heartbeats),
    ):
        return lifecycle.wait_for_world_ready(
            object(), stable_duration=0, timeout=1.3,
            panel_hotkeys=hotkeys, **kwargs,
        )


def test_complete_gameover_world_does_not_pass_allocator_gate():
    heartbeat = Mock(return_value=False)
    controls = Mock()
    controls.poll.return_value = False
    with pytest.raises(TimeoutError):
        run_wait(
            worlds=Mock(return_value=object()), heartbeats=heartbeat,
            hotkeys=controls, require_heap_callsite=True,
        )
    assert heartbeat.call_count == 2
    assert controls.poll.call_count > 20


def test_world_releases_when_allocator_pass_resumes():
    world = object()
    heartbeat = Mock(side_effect=[False, True])
    controls = Mock()
    controls.poll.return_value = False
    assert run_wait(
        worlds=Mock(return_value=world), heartbeats=heartbeat,
        hotkeys=controls, require_heap_callsite=True,
    ) is world
    assert heartbeat.call_count == 2
    assert controls.poll.call_count > 10


def test_world_change_during_heartbeat_requires_new_check():
    old, new = object(), object()
    heartbeat = Mock(return_value=True)
    assert run_wait(
        worlds=Mock(side_effect=[old, new, new, new]),
        heartbeats=heartbeat, require_heap_callsite=True,
    ) is new
    assert heartbeat.call_count == 2


def test_f10_interrupts_inactive_allocator_wait():
    controls = Mock()
    controls.poll.side_effect = [False, False, True]
    with pytest.raises(InterruptedError):
        run_wait(
            worlds=Mock(return_value=object()),
            heartbeats=Mock(return_value=False), hotkeys=controls,
            require_heap_callsite=True,
        )
    assert controls.poll.call_count == 3


def test_identity_only_callers_do_not_install_heartbeat():
    world = object()
    heartbeat = Mock()
    assert run_wait(
        worlds=Mock(return_value=world), heartbeats=heartbeat,
    ) is world
    heartbeat.assert_not_called()
