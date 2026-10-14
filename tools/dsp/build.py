#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the DMA helper and idle DSP test image with LLVM; no hardware access."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=HERE / 'build')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    tools = {name: shutil.which(name) for name in ('clang', 'ld.lld', 'llvm-objcopy')}
    if not all(tools.values()):
        parser.error('clang, ld.lld and llvm-objcopy are required')
    flags = ['--target=riscv32', '-march=rv32imaf_zicsr_zifencei', '-mabi=ilp32f',
             '-O3', '-ffreestanding', '-fno-builtin', '-nostdlib', '-mno-relax',
             '-msmall-data-limit=0', '-fno-pic', '-fno-unwind-tables',
             '-ffp-contract=fast', '-Wall', '-Wextra', '-Werror']
    objects = []
    for source in ('start.S',):
        obj = args.out / (source + '.o')
        subprocess.run([tools['clang'], *flags, '-c', str(HERE / source), '-o', str(obj)], check=True)
        objects.append(str(obj))
    script = args.out / 'dsp.ld'
    script.write_text('''ENTRY(_start)
SECTIONS {
 . = 0; .text : { KEEP(*(.text.entry)) *(.text .text.*) *(.rodata .rodata.* .srodata .srodata.*) }
 ASSERT(SIZEOF(.text) <= 65536, "ITCM overflow")
 .data : { *(.data .data.* .sdata .sdata.*) }
 .bss : { *(.bss .bss.* .sbss .sbss.*) *(COMMON) }
 ASSERT(SIZEOF(.data) == 0 && SIZEOF(.bss) == 0, "unsupported data/BSS")
 /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.note .note.*) }
}
''')
    elf, raw = args.out / 'dsp.elf', args.out / 'dsp.bin'
    subprocess.run([tools['ld.lld'], '-T', str(script), '--no-relax', '-o', str(elf), *objects], check=True)
    subprocess.run([tools['llvm-objcopy'], '-O', 'binary', '-j', '.text', str(elf), str(raw)], check=True)
    for stem, entry in (('wm-dma', 'dma_trial'),):
        wm_obj, wm_elf, wm_raw = (args.out / name for name in (stem+'.o', stem+'.elf', stem+'.bin'))
        subprocess.run([tools['clang'], '--target=riscv32', '-march=rv32im_zicsr_zifencei',
                        '-mabi=ilp32', '-O2', '-ffreestanding', '-fno-builtin', '-nostdlib',
                        '-mno-relax', '-msmall-data-limit=0', '-fno-pic', '-Wall', '-Wextra', '-Werror',
                        '-c', str(HERE/(stem+'.c')), '-o', str(wm_obj)], check=True)
        subprocess.run([tools['ld.lld'], '-Ttext=0x00910000', '-e', entry, '--no-relax',
                        '-o', str(wm_elf), str(wm_obj)], check=True)
        subprocess.run([tools['llvm-objcopy'], '-O', 'binary', '-j', '.text', str(wm_elf), str(wm_raw)], check=True)
        assert 0 < wm_raw.stat().st_size <= 2048
    sys.path.insert(0, str(HERE.parents[1] / 'firmware'))
    import container
    regions = []
    for band in range(3):
        base = 0x86000000 + (band << 22)
        regions.extend(((base, raw.read_bytes(), container.FEATURE_ENCRYPT),
                        (base+0x200000, bytes(4096), container.FEATURE_ENCRYPT)))
    image = args.out / 'w1700k_dma_test.bin'
    image.write_bytes(container.ram_container(regions, 'dma-test01', '20261014000000'))
    decoded = container.read_ram_container(image.read_bytes())
    assert [(a, len(b)) for a, b, _ in decoded] == [
        (a, (len(b)+15) & ~15) for a, b, _ in regions]
    print(json.dumps({'image': str(raw), 'bytes': raw.stat().st_size,
                      'container': str(image), 'hardware_validated': False}))

if __name__ == '__main__':
    main()
