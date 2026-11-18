#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build our MT7996 firmware set into one directory, with firmware.json describing it.

    python3 build.py [--out build]

Containers, in download order: our ROM patch (only our hooks, rom_patch/patch.py), our WM (the
radio code, the stream producer) and our WA (the stream packer), own.py. No MediaTek byte.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import own                                     # noqa: E402
from rom_patch import patch                    # noqa: E402

SCHEMA = 3


def _entry(role, name, data, flags=()):
    return {'role': role, 'file': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'flags': list(flags)}


def build(out):
    # The ROM the set is for: the host loads it onto no other (rom_patch/patch.py).
    manifest = {'schema': SCHEMA, 'rom': {'base': patch.ROM_BASE, 'size': patch.ROM_BYTES, 'sha256': list(patch.ROM_SHA256)},
                'containers': []}
    for role, name, data, flags in [('patch', 'w1700k_rom_patch.bin', patch.build_own(), ())] + own.build():
        (out / name).write_bytes(data)
        manifest['containers'].append(_entry(role, name, data, flags))
    (out / 'firmware.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, default=HERE / 'build')
    args = ap.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob('*.bin'):
        stale.unlink()
    manifest = build(out)
    for entry in manifest['containers']:
        print(f"{entry['file']:<24} {entry['size']:>8}  {entry['sha256']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
