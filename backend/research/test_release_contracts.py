"""Release metadata and the actual PowerShell map-selection handshake."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_distribution_versions_agree_with_assembly_metadata():
    props = ET.parse(ROOT/'Directory.Build.props').getroot().find('PropertyGroup')
    version = props.findtext('Version')
    assert props.findtext('AssemblyVersion') == version+'.0'
    assert props.findtext('FileVersion') == version+'.0'
    assert props.findtext('InformationalVersion') == version
    assert f"VERSION = '{version}'" in (ROOT/'scripts/build_distribution.py').read_text()
    for name in ('README.md', 'SOURCE.txt', 'THIRD-PARTY-NOTICES.txt'):
        assert version in (ROOT/name).read_text(encoding='utf-8')
    assert 'Assembly.GetName().Version' in (ROOT/'src/TheWarriorsMapTriggers.Core/BuildInfo.cs').read_text()


def wait(predicate, process):
    end = time.monotonic()+10
    while not predicate():
        assert process.poll() is None, 'Selector exited before replying'
        if time.monotonic() >= end:
            raise TimeoutError('Selector handshake timeout')
        time.sleep(.03)


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell integration')
@pytest.mark.parametrize('cancel', (False, True))
def test_powershell_selector_rejects_stale_response_and_cleans_request(cancel):
    tmp_path = ROOT / '.test-tmp' / ('handshake-' + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    text = (ROOT/'backend/research/map_triggers_collision_freecam.ps1').read_text(encoding='utf-8')
    start = text.index('function Select-VolumeEntry(')
    end = start+1+re.search(r'(?m)^function ', text[start+1:]).start()
    function = text[start:end]
    script = tmp_path/'selector.ps1'
    script.write_text('''$ErrorActionPreference = "Stop"
$NonInteractive = $true
$ownerProcess = $null
$mapRequestPath = Join-Path $PSScriptRoot "map_request.json"
$mapResponsePath = Join-Path $PSScriptRoot "map_response.txt"
$stopPath = Join-Path $PSScriptRoot "stop.flag"
'''+function+'''
$catalog = @{ entries = @(@{ id="a"; display_name="Map A"; category="story"; level_id="a"; entry=1; mission_name="A" }) }
$selected = Select-VolumeEntry $catalog "" $true
if ($null -ne $selected) { $selected.id | Set-Content (Join-Path $PSScriptRoot "selected.txt") }
''', encoding='utf-8-sig')
    response = tmp_path/'map_response.txt'
    response.write_text(json.dumps(dict(request_id='obsolete', map_id='wrong')))
    with subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            request = tmp_path/'map_request.json'
            wait(request.exists, process)
            data = json.loads(request.read_text(encoding='utf-8-sig'))
            assert re.fullmatch('[a-f0-9]{32}', data['request_id'])
            assert not response.exists()
            if cancel:
                (tmp_path/'stop.flag').touch()
            else:
                response.write_text(json.dumps(dict(request_id='obsolete', map_id='wrong')))
                wait(lambda: not response.exists(), process)
                assert not (tmp_path/'selected.txt').exists()
                response.write_text(json.dumps(dict(request_id=data['request_id'], map_id='a')))
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, err.decode(errors='replace')
            assert not request.exists()
            assert not response.exists()
            if not cancel:
                assert (tmp_path/'selected.txt').read_text().strip() == 'a'
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell integration')
@pytest.mark.parametrize('codes,attempted,abandoned,installed', [
    ([77, 0], False, False, False),
    ([78], True, False, True),
    ([79], False, True, True),
    ([80], False, True, True),
    ([0], False, False, False),
])
def test_supervisor_retries_only_confirmed_nonentry(codes, attempted, abandoned, installed):
    directory=ROOT/'.test-tmp/node_modules'/('cleanup-'+uuid.uuid4().hex)
    (directory/'research').mkdir(parents=True)
    (directory/'codes.json').write_text(json.dumps(codes))
    # A process stub, without any PINE connection or game-memory operations.
    (directory/'research/simultaneous_debug_renderer.py').write_text('''
import json
from pathlib import Path
import sys
p=Path('codes.json')
codes=json.loads(p.read_text())
code=codes.pop(0)
p.write_text(json.dumps(codes))
raise SystemExit(code)
''')
    text=(ROOT/'backend/research/map_triggers_collision_freecam.ps1').read_text(encoding='utf-8')
    start=text.index('function Invoke-RendererCleanup(')
    end=text.index('function Wait-ForStableWorld(',start)
    script=directory/'supervisor.ps1'
    script.write_text('''param([string]$Python)
$ErrorActionPreference="Stop"
$SessionDirectory=$PSScriptRoot
$python=$Python
$CollisionToggle=$true
$Port=31001
$rendererInstalled=$true
$rendererAbandoned=$false
$rendererCleanupAttempted=$false
$codePointer="0x1800000"
$dataPointer="0x1810000"
$customDataset="unused"
Set-Location $PSScriptRoot
'''+text[start:end]+'''
$cleaned=Invoke-RendererCleanup $false
@{attempted=$rendererCleanupAttempted; abandoned=$rendererAbandoned; installed=$rendererInstalled} |
    ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot "result.json")
''',encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                           '-File',str(script),'-Python',sys.executable],capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    state=json.loads((directory/'result.json').read_text(encoding='utf-8-sig'))
    assert state==dict(attempted=attempted,abandoned=abandoned,installed=installed)
    assert json.loads((directory/'codes.json').read_text())==[]
