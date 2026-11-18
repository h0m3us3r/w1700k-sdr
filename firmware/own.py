# SPDX-License-Identifier: MIT
"""Our own RAM containers for the MT7996: the WM (wm/) and the WA (wa/).

The ROM starts no core until every required RAM type has had its FW_START: its handler collects
one bit per type and compares them with a required mask a ROM routine sets to WM | WA | DSP
(retail 0x00839446). Our ROM patch's start-mask hook (rom_patch/hook-start-mask.S) counts the
DSP as started, so no DSP image is loaded. Our WA serves the stream packer the way MediaTek's WA
dispatch does (wa/wa.c).
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import container
from stream import blob

HERE = Path(__file__).resolve().parent
WM_ENTRY, WM_LIMIT = 0x00916400, 0x00938000       # ULM1, as wm/wm.h
WM_CRAM, WM_CRAM_LIMIT = 0xe0040000, 0xe027c000    # CRAM, as wm/wm.h
# The stream producer in ULM1 above our WM's code (one image, every packing schedule); the WA packer
# at the address the producer raises the WA into (stream/layout.h WA_CODE). Loaded with the firmware.
PRODUCER_BASE = 0x00930000
# Of the 0x8000 from there to WM_LIMIT. It was 0x2000, which the producer nearly fills (8056 bytes
# from clang 21, and how many depends on the compiler); an image of 8228 bytes has run on a unit.
PRODUCER_SIZE = 0x3000
WA_ENTRY, WA_LIMIT = 0x10000000, 0x1001c000       # WA ILM
VERSION = 'w1700k-own'
BUILD_DATE = '20260101000000'

ENTRY = container.FEATURE_ENCRYPT | container.FEATURE_ENTRY


# The WM's two regions: ULM1 holds start.S (at the entry) and the main loop; CRAM the radio code
# with its tables, data and zeroed BSS (in the image, so nothing clears it at start).
WM_ULM1_SOURCES = ['wm/start.S', 'wm/wm.c']
WM_CRAM_SOURCES = ['radio/stream_arm.c', 'radio/hal_wm.c', 'radio/radio.c', 'radio/patch_init.c', 'radio/rfinit.c', 'radio/bandinit.c', 'radio/dcoc.c', 'radio/capture.c', 'radio/chanset.c', 'radio/rfctl.c', 'radio/rfrun.c']
WM_LINKER_SCRIPT = (
    'SECTIONS {{\n'
    ' . = {ulm1:#x};\n'
    ' .text : {{ KEEP(*(.text.entry)) *ulm1-*.o(.text .text.* .rodata .rodata.* .srodata .srodata.*) }}\n'
    ' . = {cram:#x};\n'
    ' .cram : {{ *cram-*.o(.text .text.* .rodata .rodata.* .srodata .srodata.* .data .data.* .sdata .sdata.*)'
    ' *cram-*.o(.bss .bss.* .sbss .sbss.* COMMON) }}\n'
    ' /DISCARD/ : {{ *(.comment) *(.riscv.attributes) *(.note .note.*) }}\n'
    '}}\n')


def wm_code(keep_elf=None):
    """The WM's (ULM1 bytes, CRAM bytes); keep_elf: also copy the linked ELF there."""
    tools = {t: shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy')}
    missing = [t for t, path in tools.items() if not path]
    if missing:
        raise SystemExit('the WM build needs ' + ', '.join(missing))
    defines = blob.schedule_defines()
    defines.update(WM_PRODUCER=f'{PRODUCER_BASE:#x}u')
    flags = blob.CFLAGS + [f'-I{HERE.parent / "include"}'] + [f'-D{k}={v}' for k, v in defines.items()]
    with tempfile.TemporaryDirectory() as tmp:
        objects = []
        for kind, sources in (('ulm1', WM_ULM1_SOURCES), ('cram', WM_CRAM_SOURCES)):
            for i, source in enumerate(sources):
                obj = Path(tmp, f'{kind}-{i}.o')
                subprocess.run([tools['clang'], *flags, '-c', str(HERE / source), '-o', str(obj)], check=True)
                objects.append(str(obj))
        script, elf = Path(tmp, 'wm.ld'), Path(tmp, 'wm.elf')
        script.write_text(WM_LINKER_SCRIPT.format(ulm1=WM_ENTRY, cram=WM_CRAM))
        subprocess.run([tools['ld.lld'], '-T', str(script), '--no-relax', '-e', f'{WM_ENTRY:#x}', '-o', str(elf),
                        *objects], check=True)
        if keep_elf:
            Path(keep_elf).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(elf, keep_elf)
        sections = blob._alloc_sections(elf.read_bytes())
        if sections != ['.text', '.cram']:
            raise SystemExit(f'WM sections other than .text and .cram: {sections}')
        out = []
        for name, base, limit in (('.text', WM_ENTRY, PRODUCER_BASE), ('.cram', WM_CRAM, WM_CRAM_LIMIT)):
            raw = Path(tmp, name.strip('.') + '.bin')
            subprocess.run([tools['llvm-objcopy'], '-O', 'binary', '-j', name, str(elf), str(raw)], check=True)
            code = raw.read_bytes()
            code += bytes(-len(code) % 16)
            if len(code) > limit - base:
                raise SystemExit(f'WM {name} too large: {len(code)} bytes, {limit - base} allowed')
            out.append(code)
    return tuple(out)


def wa_code():
    """The WA: start.S, then the dispatch loop that enters the stream packer."""
    return blob.compile_blob([str(HERE / 'wa' / 'start.S'), str(HERE / 'wa' / 'wa.c')], {}, WA_ENTRY, blob.WA_CODE)


def producer():
    return blob.compile_blob(['lead.S', 'producer.c', 'pack.S', 'reduce14.S'], blob.schedule_defines(), PRODUCER_BASE,
                             PRODUCER_BASE + PRODUCER_SIZE)


def wm_container():
    ulm1, cram = wm_code(HERE / 'build' / 'w1700k_wm.elf')
    regions = [(WM_ENTRY, ulm1, ENTRY)]
    regions.append((PRODUCER_BASE, producer(), container.FEATURE_ENCRYPT))
    regions.append((WM_CRAM, cram, container.FEATURE_ENCRYPT))
    return container.ram_container(regions, VERSION, BUILD_DATE)


def wa_container():
    packer = blob.wa_packer()
    return container.ram_container([(WA_ENTRY, wa_code(), ENTRY), (blob.WA_CODE, packer, container.FEATURE_ENCRYPT)],
                                   VERSION, BUILD_DATE)


def build():
    """[(role, file name, container bytes, download flags)] in download order (after the patch)."""
    return [
        ('wm', 'w1700k_wm.bin', wm_container(), ()),
        ('wa', 'w1700k_wa.bin', wa_container(), ('--wa',)),
    ]
