# SPDX-License-Identifier: MIT
"""Compile the stream firmware into position-fixed images: the WM producer and the WA packer,
which own.py places in our WM's and WA's containers at their link addresses.

An image has no loader: our WM enters the producer as a function, our WA the packer. So
everything must land in .text (code, then read-only data); any data, bss or small-data section
is refused, as is a call into a runtime library. ld.lld resolves every relocation at link time
and llvm-objcopy extracts the raw .text.

One producer and one packer serve every packing schedule; a stream's STREAM command names the
one it runs (radio/radio_cmd.h RADIO_PACKING_*, in PACKINGS' order). Over each PERIOD chunks:
- raw: no packing; every 8 KiB chunk is published as captured (up to 160 Msps of 14-bit IQ on
  one 10 GbE link).
- half: the WM packs 8 chunks and the WA 8 of every 32 to 28 bits (7168 bytes): 50% packed,
  so 320 Msps fits one 10 GbE link.
- full: the WM packs 16 and the WA 16 of every 32, every chunk: 7/8 of the bytes, lossless, for
  links slower than the samples (160 Msps in 4.5 Gbit/s). Each packer then packs as many chunks
  a second as in half at twice the sample rate, so it carries up to 160 Msps: there, with both
  reading the capture ring all the time, a chunk takes either 11.4k cycles and each is 62 % busy.
- split: 20 of every 32 reduced to 14 bits (reduce14.S), 12 left raw for the SoC: 320 Msps on
  5 Gbit/s. The WM takes 9 of the 20 and the WA 11, not 10 each. The WM also publishes every
  chunk and runs the control programs, and at 10 each it had about 0.2 % of its time to spare
  at 320 Msps: a retune took 0.3 to 3.9 s, and a few instructions more in its loop overran the
  stream. At 9 and 11 a retune takes what it takes at 160 Msps (1.9 ms), the capture is at most
  74K ahead where it was 78K to 142K, and the WA, which only packs, is 97 % busy. One chunk
  more to the WA (6 and 4 of 16) is more than it can do. [measured]
The positions of the chunks a packing leaves raw are the same as when the period was 16: what
changed is which core packs which of the others, and that is the cores' own affair.
Raising the WA (half, full) leaves the firmware command path dead until the next firmware load.
"""
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

WA_CODE, WA_LIMIT = 0x1001A400, 0x1001B000      # 1 KiB below WA_STACK_TOP stays the WA's stack

CFLAGS = ['--target=riscv32', '-march=rv32ima_zicsr', '-mabi=ilp32', '-O2', '-ffreestanding', '-fno-builtin',
          '-nostdlib', '-mno-relax', '-msmall-data-limit=0', '-mcmodel=medany', '-fno-pic', '-fno-jump-tables',
          '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-Wall', '-Werror']
LINKER_SCRIPT = ('SECTIONS {{\n . = {base:#x};\n .text : {{ KEEP(*(.text.entry)) *(.text .text.*) '
                 '*(.rodata .rodata.* .srodata .srodata.*) }}\n /DISCARD/ : {{ *(.comment) *(.riscv.attributes) '
                 '*(.note .note.*) }}\n}}\n')


@dataclass(frozen=True)
class Packing:
    name: str
    wa: int                       # chunks of each period the WA packs
    wm: int                       # chunks of each period the WM packs
    reduce: bool = False          # packed chunks are reduced to 14 bits (reduce14.S), not packed to 28


PERIOD = 32
RAW = Packing('raw', 0, 0)
HALF = Packing('half', 8, 8)
FULL = Packing('full', 16, 16)
SPLIT = Packing('split', 11, 9, True)
PACKINGS = (RAW, HALF, FULL, SPLIT)   # index = the RADIO_PACKING_* code


def schedule(packing, period=PERIOD):
    """(WA mask, WM mask, slot table): each packer's positions spread evenly over the period."""
    wa, wm = packing.wa, packing.wm
    n = wa + wm
    if period & (period - 1) or not 1 <= period <= 32 or n > period:
        raise ValueError('period: a power of two up to 32, at least wa + wm')
    if not n:
        return 0, 0, [0] * period
    at = [i * period // n for i in range(n)]
    who = ['wa' if (j + 1) * wa // n > j * wa // n else 'wm' for j in range(n)]
    masks, slots, count = dict(wa=0, wm=0), [0] * period, dict(wa=0, wm=0)
    for position, packer in zip(at, who):
        masks[packer] |= 1 << position
        slots[position] = count[packer]
        count[packer] += 1
    return masks['wa'], masks['wm'], slots


def schedule_defines():
    """-D PERIOD and SCHEDULES: stream.h's table, one struct sched initialiser per packing."""
    rows = []
    for packing in PACKINGS:
        wa_mask, wm_mask, slots = schedule(packing)
        rows.append(f"{{{wa_mask:#x}u,{wm_mask:#x}u,{packing.wa}u,{packing.wm}u,{int(packing.reduce)}u,"
                    f"{{{','.join(map(str, slots))}}}}}")
    return dict(PERIOD=f'{PERIOD}u', SCHEDULES=','.join(rows))


def _alloc_sections(elf):
    """Names of an ELF32's non-empty allocated sections."""
    shoff, = struct.unpack_from('<I', elf, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', elf, 0x2e)
    headers = [struct.unpack_from('<IIIIIIIIII', elf, shoff + i * shentsize) for i in range(shnum)]
    strtab = headers[shstrndx][4]
    name = lambda off: elf[strtab + off:elf.index(b'\0', strtab + off)].decode()
    return [name(h[0]) for h in headers if h[2] & 2 and h[5]]


def compile_blob(sources, defines, base, limit):
    """Compile and link sources into one blob at `base`, its entry (section .text.entry) first."""
    tools = {t: shutil.which(t) for t in ('clang', 'ld.lld', 'llvm-objcopy')}
    missing = [t for t, path in tools.items() if not path]
    if missing:
        raise SystemExit('the stream firmware build needs ' + ', '.join(missing))
    flags = CFLAGS + [f'-I{HERE.parents[1] / "include"}'] + [f'-D{k}={v}' for k, v in defines.items()]
    with tempfile.TemporaryDirectory() as tmp:
        objects = []
        for i, source in enumerate(sources):
            obj = Path(tmp, f'{i}.o')
            subprocess.run([tools['clang'], *flags, '-c', str(HERE / source), '-o', str(obj)], check=True)
            objects.append(str(obj))
        script, elf, raw = Path(tmp, 'blob.ld'), Path(tmp, 'blob.elf'), Path(tmp, 'blob.bin')
        script.write_text(LINKER_SCRIPT.format(base=base))
        subprocess.run([tools['ld.lld'], '-T', str(script), '--no-relax', '-e', f'{base:#x}', '-o', str(elf),
                        *objects], check=True)
        sections = _alloc_sections(elf.read_bytes())
        if sections != ['.text']:
            raise SystemExit(f'blob sections other than .text: {sections}')
        subprocess.run([tools['llvm-objcopy'], '-O', 'binary', '-j', '.text', str(elf), str(raw)], check=True)
        code = raw.read_bytes()
    code += bytes(-len(code) % 4)
    if len(code) > limit - base:
        raise SystemExit(f'blob too large: {len(code)} bytes, {limit - base} allowed')
    return code


def wa_packer():
    return compile_blob(['wa_lead.S', 'packer.c', 'pack.S', 'reduce14.S'], schedule_defines(), WA_CODE, WA_LIMIT)
