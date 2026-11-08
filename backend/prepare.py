"""Build a private geometry cache from the user's supported disc image."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent / 'research'))
from inspect_warriors_archive import read_root_entries
from catalog_lua_volumes import scan, write_datasets

CACHE_VERSION = 1
ELF_SHA256 = '1a4e1c3891c930146e39ce340ccb02918cfe917032aafcdbbbe6541f75402369'


def image_identity(iso):
    with iso.open('rb') as handle:
        entries = read_root_entries(handle)
        if not {'SLUS_212.15', 'WARRIORS.DIR', 'WARRIORS.WAD'} <= entries.keys():
            raise ValueError('Select The Warriors USA SLUS-21215 v1.03 ISO9660 image.')
        executable = entries['SLUS_212.15']
        handle.seek(executable.offset)
        elf_hash = hashlib.sha256(handle.read(executable.size)).hexdigest()
        if elf_hash != ELF_SHA256:
            raise ValueError('This image has an unsupported or modified game executable.')
        # A complete image digest also invalidates cached Lua data when a WAD
        # changes, even if the executable and directory remain identical.
        handle.seek(0)
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def valid_cache(target, identity):
    try:
        manifest = json.loads((target / 'cache.json').read_text(encoding='utf-8'))
        if manifest['version'] != CACHE_VERSION or manifest['image_sha256'] != identity:
            return False
        for relative, expected in manifest['files'].items():
            path = (target / relative).resolve()
            if not path.is_relative_to(target.resolve()):
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        catalog = json.loads((target / 'lua_volume_catalog.json').read_text(encoding='utf-8'))
        required = {'lua_volume_catalog.json', *(entry['dataset'] for entry in catalog['entries'])}
        return required <= manifest['files'].keys() and bool(catalog['entries'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prepare(iso: Path, cache: Path):
    iso = iso.resolve(strict=True)
    identity = image_identity(iso)
    target = cache.resolve() / f'v{CACHE_VERSION}-{identity}'
    if not valid_cache(target, identity):
        cache.mkdir(parents=True, exist_ok=True)
        # Inherit the user's cache ACL. A Windows mode-0700 tempfile can exclude
        # restricted child tokens even when their parent just created it.
        staging = cache / ('building-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            catalog, volumes, flags = scan(iso)
            if catalog['parse_errors'] or not catalog['entries']:
                raise ValueError('The image scripts could not be extracted completely.')
            write_datasets(catalog, volumes, flags, staging / 'volumes')
            (staging / 'lua_volume_catalog.json').write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            files = {path.relative_to(staging).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in staging.rglob('*.json')}
            (staging / 'cache.json').write_text(json.dumps(dict(
                version=CACHE_VERSION, image_sha256=identity, files=files)), encoding='utf-8')
            if target.exists():
                # Only a digest-named child of the requested cache can be removed.
                if target.parent != cache.resolve():
                    raise ValueError('Cache target escaped its directory')
                shutil.rmtree(target)
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return dict(iso=str(iso), catalog=str(target / 'lua_volume_catalog.json'), image_sha256=identity)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--iso', type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    if args.smoke_test:
        import tkinter
        import ctypes
        import native_freecam, simultaneous_debug_renderer, controller_input, trigger_overlay
        interpreter = tkinter.Tcl()
        print(json.dumps(dict(python=sys.version, pointer_bits=ctypes.sizeof(ctypes.c_void_p)*8,
                              tcl=interpreter.call('info', 'patchlevel'), imports='ok')))
        return
    if args.iso is None or args.cache is None:
        parser.error('--iso and --cache are required')
    print(json.dumps(prepare(args.iso, args.cache)))


if __name__ == '__main__':
    main()
