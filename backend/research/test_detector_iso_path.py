from pathlib import Path

import detect_current_map as detector


def test_isolated_branch_resolves_shared_iso_independent_of_cwd(tmp_path, monkeypatch):
    root = tmp_path / 'TheWarriors'
    script = root / 'experiments' / 'native-camera' / 'research' / 'detect_current_map.py'
    script.parent.mkdir(parents=True)
    shared = root / 'Warriors, The (USA).iso'
    shared.write_bytes(b'test disc path')
    monkeypatch.setattr(detector, '__file__', str(script))
    monkeypatch.chdir(tmp_path)
    assert detector.default_iso_path() == shared


def test_closest_project_image_takes_precedence(tmp_path, monkeypatch):
    script = tmp_path / 'project' / 'research' / 'detect_current_map.py'
    script.parent.mkdir(parents=True)
    for directory in (tmp_path, script.parent.parent):
        (directory / 'Warriors, The (USA).iso').write_bytes(b'test')
    monkeypatch.setattr(detector, '__file__', str(script))
    assert detector.default_iso_path() == script.parent.parent / 'Warriors, The (USA).iso'


def test_without_iso_uses_supported_memory_only_detection(tmp_path, monkeypatch):
    script = tmp_path / 'research' / 'detect_current_map.py'
    monkeypatch.setattr(detector, '__file__', str(script))
    monkeypatch.setattr(Path, 'is_file', lambda self: False)
    assert detector.default_iso_path() is None
