# SPDX-License-Identifier: MIT
"""Our own RAM containers for the MT7996: the WM (wm/) and the WA (wa/).

The ROM starts no core until every required RAM type has had its FW_START: its handler collects
one bit per type and compares them with a required mask a ROM routine sets to WM | WA | DSP
(retail 0x00839446). Our ROM patch's start-mask hook (rom_patch/hook-start-mask.S) counts the
DSP as started, so a set without a DSP image starts too. Our WA serves the stream packer the way
MediaTek's WA dispatch does (wa/wa.c).

The DSP image (dsp/) is the spectrum occupancy program, the same on each of the three DSP cores:
its code in a core's ITCM, its tables in the DTCM. Our WM feeds core 0 from the stream
(stream/occ.h); the other two run it and are asked nothing.
"""
import math
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

# The DSP cores' memories as the download engine addresses them: core b's 64 KiB ITCM and DTCM
# (include/hw/mt7996/dsp.h MT7996_DSP_MCU_ITCM / _DTCM). The core itself has them at 0 and DSP_OWN_DTCM.
DSP_CORES = 3
DSP_ITCM, DSP_DTCM, DSP_TCM_SIZE = 0x86000000, 0x86200000, 0x10000
DSP_OWN_DTCM = 0x00200000
DSP_DATA, DSP_REPORT = 0x0100, 0x8000              # dsp/occ_layout.h OCC_DATA, OCC_REPORT
DSP_BINS = 64                                      # dsp/occ_layout.h OCC_BINS
DSP_CFLAGS = ['--target=riscv32', '-march=rv32imaf_zicsr_zifencei', '-mabi=ilp32f', '-O3', '-ffreestanding',
              '-fno-builtin', '-nostdlib', '-mno-relax', '-msmall-data-limit=0', '-fno-pic', '-fno-unwind-tables',
              '-fno-asynchronous-unwind-tables', '-ffp-contract=fast', '-Wall', '-Wextra', '-Werror']
DSP_LINKER_SCRIPT = (
    'ENTRY(_start)\n'
    'SECTIONS {{\n'
    ' . = 0;\n'
    ' .text : {{ KEEP(*(.text.entry)) *(.text .text.*) }}\n'
    ' ASSERT(SIZEOF(.text) <= {tcm:#x}, "the DSP program\'s code does not fit the ITCM")\n'
    ' . = {data:#x};\n'
    ' .data : {{ *(.rodata .rodata.* .srodata .srodata.*) *(.data .data.* .sdata .sdata.*) }}\n'
    ' . = ALIGN(4);\n'
    ' __bss_start = .;\n'
    ' .bss : {{ *(.bss .bss.* .sbss .sbss.*) *(COMMON) }}\n'
    ' . = ALIGN(4);\n'
    ' __bss_end = .;\n'
    ' ASSERT(__bss_end <= {report:#x}, "the DSP program\'s tables and working memory run into its report")\n'
    ' /DISCARD/ : {{ *(.comment) *(.riscv.attributes) *(.note .note.*) *(.eh_frame*) }}\n'
    '}}\n')

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


def dsp_tables(bins=DSP_BINS):
    """dsp/occ.c's tables.h: the Hann window, the transform's twiddles (e^(-2 pi i k / bins), the first
    half turn) and the bit-reversed index of each input."""
    bits = bins.bit_length() - 1
    if 1 << bits != bins:
        raise ValueError('bins: a power of two')
    row = lambda values: ', '.join(f'{v:.9e}f' for v in values)
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * n / bins) for n in range(bins)]
    turn = [2 * math.pi * k / bins for k in range(bins // 2)]
    rev = [int(f'{n:0{bits}b}'[::-1], 2) for n in range(bins)]
    return ('/* Generated by firmware/own.py dsp_tables. */\n'
            f'static const float hann[{bins}] = {{ {row(hann)} }};\n'
            f'static const float tw_re[{bins // 2}] = {{ {row(math.cos(a) for a in turn)} }};\n'
            f'static const float tw_im[{bins // 2}] = {{ {row(-math.sin(a) for a in turn)} }};\n'
            f'static const unsigned char rev[{bins}] = {{ {", ".join(map(str, rev))} }};\n')


def dsp_code(keep_elf=None):
    """The DSP program's (ITCM bytes, DTCM bytes from the DTCM's start): its code, and its tables at
    DSP_DATA behind a cleared box and request block. The working memory above the tables is cleared
    by the program's own start."""
    tools = {t: shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy')}
    missing = [t for t, path in tools.items() if not path]
    if missing:
        raise SystemExit('the DSP build needs ' + ', '.join(missing))
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'tables.h').write_text(dsp_tables())
        objects = []
        for source in ('start.S', 'occ.c'):
            obj = Path(tmp, source + '.o')
            subprocess.run([tools['clang'], *DSP_CFLAGS, f'-I{tmp}', f'-I{HERE / "dsp"}', '-c',
                            str(HERE / 'dsp' / source), '-o', str(obj)], check=True)
            objects.append(str(obj))
        script, elf = Path(tmp, 'dsp.ld'), Path(tmp, 'dsp.elf')
        script.write_text(DSP_LINKER_SCRIPT.format(tcm=DSP_TCM_SIZE, data=DSP_OWN_DTCM + DSP_DATA,
                                                   report=DSP_OWN_DTCM + DSP_REPORT))
        subprocess.run([tools['ld.lld'], '-T', str(script), '--no-relax', '-o', str(elf), *objects], check=True)
        if keep_elf:
            Path(keep_elf).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(elf, keep_elf)
        sections = blob._alloc_sections(elf.read_bytes())
        if sections != ['.text', '.data', '.bss']:
            raise SystemExit(f'DSP sections other than .text, .data and .bss: {sections}')
        out = []
        for name in ('.text', '.data'):
            raw = Path(tmp, name.strip('.') + '.bin')
            subprocess.run([tools['llvm-objcopy'], '-O', 'binary', '-j', name, str(elf), str(raw)], check=True)
            code = raw.read_bytes()
            out.append(code + bytes(-len(code) % 16))
    return out[0], bytes(DSP_DATA) + out[1]


def dsp_container():
    itcm, dtcm = dsp_code(HERE / 'build' / 'w1700k_dsp.elf')
    regions = []
    for core in range(DSP_CORES):
        regions += [(DSP_ITCM + (core << 22), itcm, container.FEATURE_ENCRYPT),
                    (DSP_DTCM + (core << 22), dtcm, container.FEATURE_ENCRYPT)]
    return container.ram_container(regions, VERSION, BUILD_DATE)


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
        ('dsp', 'w1700k_dsp.bin', dsp_container(), ('--dsp',)),
        ('wa', 'w1700k_wa.bin', wa_container(), ('--wa',)),
    ]
