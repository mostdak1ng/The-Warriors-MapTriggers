"""Build local MapTriggers artifacts. Never invokes Git or a publishing API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.3.2'
RUNTIME_VERSION = '8.0.15'


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True, **kwargs)


def copy_tree(source, target, excluded=()):
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if any(part in {'bin', 'obj', '__pycache__', '.pytest_cache', *excluded} for part in relative.parts):
            continue
        if path.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def zip_tree(source, target):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent).as_posix())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python-home', type=Path, default=Path(sys.base_prefix))
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--stage-only', action='store_true', help='Build a runnable folder without creating release archives')
    parser.add_argument('--skip-tests', action='store_true', help='Incremental developer build only; recorded in build.json')
    parser.add_argument('--build-name', default=f'build-{VERSION}', help='New local staging folder; existing builds are preserved')
    args = parser.parse_args()
    # SDK advertising refresh spawned unrelated workload verifiers even for
    # an offline build. Keep this build limited to its pinned local inputs.
    os.environ['DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE'] = 'true'
    # Keep staging beneath a dependency directory excluded by common source
    # discovery tools. This parent is absent from the packaged layout.
    staging_parent = ROOT / '.maptriggers-staging' / 'node_modules'
    staging = staging_parent / args.build_name
    if staging.resolve().parent != staging_parent.resolve():
        raise ValueError('Staging escaped the project')
    if staging.exists():
        raise FileExistsError(f'Build already exists; use a new --build-name: {staging}')
    binary = staging / f'TheWarriorsMapTriggers-v{VERSION}-windows-x64'
    source = staging / f'TheWarriorsMapTriggers-v{VERSION}-source'
    binary.mkdir(parents=True); source.mkdir()
    if not args.skip_tests:
        run('dotnet', 'run', '--project', 'tests/TheWarriorsMapTriggers.Tests', '-c', 'Release', '--disable-build-servers', '-p:UseSharedCompilation=false')
        (ROOT / '.test-tmp').mkdir(exist_ok=True)
        run(sys.executable, '-m', 'pytest', 'backend', '-q', '-p', 'no:cacheprovider', '--basetemp=.test-tmp/build')
    project = ROOT / 'src/TheWarriorsMapTriggers/TheWarriorsMapTriggers.csproj'
    restore = ['dotnet', 'restore', project, '-r', 'win-x64', f'-p:RuntimeFrameworkVersion={RUNTIME_VERSION}', '-p:TargetLatestRuntimePatch=false', '-p:NuGetAudit=false']
    if args.offline:
        restore += ['--source', ROOT / '.offline-packages', '--ignore-failed-sources']
    run(*restore)
    run('dotnet', 'publish', project, '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '--no-restore',
        '--disable-build-servers', '-m:1', '-p:UseSharedCompilation=false',
        f'-p:RuntimeFrameworkVersion={RUNTIME_VERSION}', '-p:TargetLatestRuntimePatch=false',
        '-p:PublishSingleFile=true', '-p:IncludeNativeLibrariesForSelfExtract=true', '-p:DebugType=embedded', '-o', binary)
    runtime = binary / 'runtime'
    runtime.mkdir()
    pyhome = args.python_home.resolve()
    identity = subprocess.check_output([str(pyhome / 'python.exe'), '-I', '-B', '-c',
        'import sys,struct,json;print(json.dumps([list(sys.version_info[:3]),struct.calcsize("P")]))'], text=True)
    if json.loads(identity) != [[3, 14, 4], 8]:
        raise ValueError('The distribution requires the documented CPython 3.14.4 x64 runtime')
    for filename in ('python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt'):
        shutil.copy2(pyhome / filename, runtime / filename)
    for directory in ('Lib', 'DLLs', 'tcl'):
        copy_tree(pyhome / directory, runtime / directory,
                  {'site-packages', 'test', 'tests', 'idlelib', 'ensurepip', 'turtledemo'})
    (runtime / 'python314._pth').write_text('.\nLib\nDLLs\n../backend\n../backend/research\n', encoding='utf-8')
    # Explicit code-only allowlist: do not ship catalogs, datasets, local state,
    # savestates, emulator files, research evidence or images.
    (binary / 'backend/research').mkdir(parents=True)
    shutil.copy2(ROOT / 'backend/prepare.py', binary / 'backend/prepare.py')
    for path in (ROOT / 'backend/research').iterdir():
        if path.suffix in {'.py', '.ps1'} and not path.name.startswith('test_'):
            shutil.copy2(path, binary / 'backend/research' / path.name)
    documents = ('README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD-PARTY-NOTICES.txt', 'SOURCE.txt')
    for name in documents:
        shutil.copy2(ROOT / name, binary / name)
        shutil.copy2(ROOT / name, source / name)
    for directory in ('src', 'tests', 'backend', 'docs'):
        copy_tree(ROOT / directory, source / directory)
    (source / 'scripts').mkdir()
    for name in ('build-release.ps1', 'build_distribution.py'):
        shutil.copy2(ROOT / 'scripts' / name, source / 'scripts' / name)
    copy_tree(ROOT / 'docs', binary / 'docs')
    shutil.copy2(ROOT / 'Directory.Build.props', source / 'Directory.Build.props')
    build = dict(version=VERSION, tests_run=not args.skip_tests,
                 python='.'.join(map(str, sys.version_info[:3])), dotnet_runtime=RUNTIME_VERSION,
                 upstream=json.loads((ROOT / 'backend/upstream-baseline.json').read_text()))
    (binary / 'build.json').write_text(json.dumps(build, indent=2), encoding='utf-8')
    (source / 'build.json').write_text(json.dumps(build, indent=2), encoding='utf-8')
    # Run with an empty PATH and a foreign working directory, using only the
    # bundled runtime and isolated search path. No installed Python or .NET.
    environment = dict(os.environ)
    environment.update(PATH='', PYTHONHOME='invalid', PYTHONPATH='invalid')
    completed = subprocess.run([str(runtime / 'python.exe'), '-B', str(binary / 'backend/prepare.py'), '--smoke-test'],
                               cwd=staging, env=environment, check=True, text=True, capture_output=True)
    print(completed.stdout.strip())
    executable_environment = dict(environment)
    executable_environment['DOTNET_ROOT'] = 'invalid'
    executable_environment['TW_MAPTRIGGERS_DATA'] = str(staging / 'diagnostic-data')
    diagnostics = staging / 'launcher-diagnostics.json'
    subprocess.run([str(binary / 'TheWarriorsMapTriggers.exe'), '--diagnostics', str(diagnostics)],
                   cwd=staging, env=executable_environment, check=True, timeout=30)
    if json.loads(diagnostics.read_text())['imports'] != 'ok':
        raise RuntimeError('Packaged launcher diagnostics failed')
    files = {path.relative_to(binary).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in binary.rglob('*') if path.is_file()}
    (binary / 'manifest.json').write_text(json.dumps(files, indent=2), encoding='utf-8')
    if args.stage_only:
        print(f'Runnable folder: {binary}')
        return
    artifacts = ROOT / 'artifacts/maptriggers-local' / f'v{VERSION}'
    artifacts.mkdir(parents=True, exist_ok=True)
    if any(artifacts.iterdir()):
        raise FileExistsError(f'Artifacts already exist and will not be replaced: {artifacts}')
    archives = []
    for tree in (binary, source):
        archive = artifacts / (tree.name + '.zip')
        zip_tree(tree, archive); archives.append(archive)
    (artifacts / 'SHA256SUMS.txt').write_text(''.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in archives), encoding='ascii')
    print(f'Local artifacts: {artifacts}\nRunnable folder: {binary}')


if __name__ == '__main__':
    main()
