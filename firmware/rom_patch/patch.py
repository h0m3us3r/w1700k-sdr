# SPDX-License-Identifier: MIT
"""Our MT7996 ROM patch: a patch container that holds only our instruction-redirect hooks.

The patch carries a 64-entry hardware redirect table (`s_hw_entry_table`, 520 bytes at the start
of its decrypted body): an enable bitmap, then from[64] and to[64]. The ROM's
SYSpatch_enable_hw_entry programs one address comparator pair per enabled entry, so a fetch at
`from` continues at `to`. The patch stage enforces no signature on this part, so the table and
body are ours to extend.

Our container starts from an empty hook table and a blank footer (empty_container). A hook
changes exactly three table words (bitmap, from[n], to[n] for the first free index n) and
inserts its replacement code between the body and the 0x400-byte security footer. The ROM
copies the whole decrypted section to patch SRAM at 0x00900000, so the replacement lands at a
known address and the footer keeps its position relative to the end; `_verify` re-decodes the
output and checks that only the table words and the appended code changed.

A replacement is: our code, then a copy of the hooked instruction, then an absolute jump to
`from + len`, so the hooked function continues in ROM. The copy must be position-independent
(no pc-relative operand); the hook sources document their own sites.
"""
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import aes

HERE = Path(__file__).resolve().parent

PATCH_SRAM_BASE = 0x00900000
PATCH_SRAM_END = 0x00910000
STATUS_PAGE = 0x0090A000          # hook.S status words; the patch copy must stay below it
STATUS_MAGIC = 0x4B4F4F48         # 'HOOK'
ROM_BASE, ROM_SIZE = 0x00800000, 0x4604C
TABLE_BYTES = 0x208
FROM_OFF, TO_OFF, N_ENTRY = 0x008, 0x108, 64
FOOTER_LEN = 0x400
SEC_OFF, SEC_SIZE, SEC_ADDR, SEC_LEN = 0x64, 0x68, 0x6C, 0x70
CODE_LIMIT = 0x100
POOL_BANKS = 704                  # the UMAC SYSRAM pool in 2 KiB banks


# The ROM everything here is written for. A hook is a ROM instruction's address, the bytes found
# there, and ROM functions called by address; the WM calls ROM routines by address too
# (include/hw/mt7996/rom_patch.h). In another revision of the ROM those addresses are the middle
# of something else, and among what the ROM holds is a routine that programs eFuses. So the host
# reads the ROM before it downloads this patch, and loads onto no ROM but one named here
# (host/w1700k/radio.py).
#
# What is hashed is the ROM's first 64 KiB, the part the driver lets a host read
# (kernel/w1700k_sdr/mmio.c). The ROM is longer; a revision that differs from this one only
# past that would pass. [measured: one HW 2.1 unit, read twice before any download, 2026-11-17]
ROM_BASE, ROM_BYTES = 0x00800000, 0x10000
ROM_SHA256 = ('596f4aeb590f1765980eefedffa39e70585f1a3249ac0b936585ecc0f1e38c07',)


@dataclass(frozen=True)
class Hook:
    source: str                   # replacement body, relative to this directory
    from_addr: int                # ROM instruction redirected
    insn: bytes                   # its bytes in memory order: the copy the replacement carries
    scratch: str = 't1'           # register the return jump is built in
    calls: dict = field(default_factory=dict)   # NAME -> ROM function the body calls
    banks: tuple = ()             # hook.S: ICAP, PLE, PSE SYSRAM banks


# Forces halWfdmaPdaCheckCRC's result to "match", so our RAM containers load (their CRC is ours,
# not the vendor key's). The PDA idle and length checks keep refusing a download that did not land.
PDA_CRC = Hook('hook-pdacrc-result.S', 0x008264E4, bytes.fromhex('13351500'), scratch='a5')

# Repartitions the UMAC SYSRAM pool at init: ICAP 512 banks at position 0 (pool bytes 0..1 MiB =
# MCU 0xe0400000..0xe04fffff, the ring the stream producer captures into), PLE 64, PSE 128.
SYSRAM_LAYOUT = Hook('hook.S', 0x00822B94, bytes.fromhex('ab8ca142'),
                     calls={'LAYOUT': 0x00822A2E, 'BANKS': 0x00822A50}, banks=(512, 64, 128))


# The ROM starts the cores once the WM and the WA have had their FW_START, so no DSP image is
# loaded.
START_MASK = Hook('hook-start-mask.S', 0x00838D6C, bytes.fromhex('ab8f8152'))
OWN = (PDA_CRC, SYSRAM_LAYOUT, START_MASK)


def _be32(blob, off):
    return struct.unpack_from('>I', blob, off)[0]


def _split_hi_lo(address):
    """The lui/jalr immediate pair for an absolute address."""
    low = address & 0xFFF
    if low >= 0x800:
        low -= 0x1000
    return (address - low) >> 12, low


def _insn_length(first_byte):
    """RISC-V instruction length from its first byte: 2 unless bits[1:0] = 11."""
    return 4 if (first_byte & 3) == 3 else 2


def read_container(blob, label):
    """(section file offset, decrypted section) of a one-section patch container."""
    if len(blob) < 0xA0:
        raise SystemExit(f'{label}: too short for a patch container')
    if _be32(blob, 44) != 1:
        raise SystemExit(f'{label}: {_be32(blob, 44)} patch sections; expected the one-section form')
    offset, size = _be32(blob, SEC_OFF), _be32(blob, SEC_SIZE)
    address, length = _be32(blob, SEC_ADDR), _be32(blob, SEC_LEN)
    if address != PATCH_SRAM_BASE:
        raise SystemExit(f'{label}: section loads at {address:#x}, not patch SRAM')
    if size != length or offset + length != len(blob) or length % 16:
        raise SystemExit(f'{label}: section bounds {offset:#x}/{size:#x}/{length:#x} do not cover '
                         'the file as one AES block run')
    plain = aes.cbc_zero(blob[offset:offset + length], encrypt=False)
    if len(plain) < TABLE_BYTES + FOOTER_LEN:
        raise SystemExit(f'{label}: decrypted section has no room for table and footer')
    if struct.unpack_from('<II', plain, len(plain) - FOOTER_LEN) != (0x40, 0x300):
        raise SystemExit(f'{label}: unexpected security footer layout')
    return offset, plain


def _free_index(bitmap_lo, bitmap_hi):
    """The next entry index. The ROM programs entries 0..(32 - clz(bitmap)) - 1 whatever the enable
    bits say, so a gap would be an entry built from unwritten words: the table must stay packed."""
    if bitmap_hi:
        raise SystemExit('the high bitmap word is in use; only entries below 32 are appended')
    if bitmap_lo & (bitmap_lo + 1):
        raise SystemExit(f'enable bitmap {bitmap_lo:#010x} is not packed from index 0')
    n = bitmap_lo.bit_length()
    if n >= 32:
        raise SystemExit('no free entry below 32')
    return n


def _assemble(source, defines):
    """Assemble a hook body to raw bytes. llvm-objcopy applies no relocations, so a relocation
    left in the object would reach the MCU as a branch to itself: refuse any."""
    tools = {t: shutil.which(t) for t in ('clang', 'llvm-objcopy', 'llvm-readobj')}
    missing = [t for t, path in tools.items() if not path]
    if missing:
        raise SystemExit('the ROM patch build needs ' + ', '.join(missing))
    with tempfile.TemporaryDirectory() as tmp:
        obj, raw = Path(tmp, 'hook.o'), Path(tmp, 'hook.bin')
        subprocess.run([tools['clang'], '--target=riscv32', '-march=rv32imc', '-mno-relax',
                        '-x', 'assembler-with-cpp', *[f'-D{k}={v}' for k, v in defines.items()],
                        '-c', str(HERE / source), '-o', str(obj)], check=True)
        relocs = subprocess.run([tools['llvm-readobj'], '--relocations', str(obj)],
                                check=True, capture_output=True, text=True).stdout
        if '.rela.text' in relocs or '.rel.text' in relocs:
            raise SystemExit(f'{source}: relocations against .text; keep every address an '
                             'assembled-in constant')
        subprocess.run([tools['llvm-objcopy'], '-O', 'binary', '--only-section=.text', str(obj), str(raw)],
                       check=True)
        code = raw.read_bytes()
    if not code or len(code) > CODE_LIMIT:
        raise SystemExit(f'{source} assembled to {len(code)} bytes, outside (0, {CODE_LIMIT}]')
    return code


def _defines(hook):
    length = len(hook.insn)
    if not ROM_BASE <= hook.from_addr < ROM_BASE + ROM_SIZE - length:
        raise SystemExit(f'{hook.from_addr:#010x} is outside the ROM')
    if _insn_length(hook.insn[0]) != length:
        raise SystemExit(f'{hook.insn.hex()} decodes as a {_insn_length(hook.insn[0])}-byte instruction')
    ret_hi, ret_lo = _split_hi_lo(hook.from_addr + length)
    defines = {'MARK_HI': f'{STATUS_PAGE >> 12:#x}', 'MARK_MAGIC': f'{STATUS_MAGIC:#x}',
               'RET_HI': f'{ret_hi:#x}', 'RET_LO': f'{ret_lo:d}', 'COPY_LEN': f'{length:d}',
               'COPY_WORD': f'{int.from_bytes(hook.insn, "little"):#x}', 'SCRATCH': hook.scratch}
    if hook.banks:
        icap, ple, pse = hook.banks
        if icap + ple + pse != POOL_BANKS or min(hook.banks) <= 0 or icap > 512:
            raise SystemExit(f'banks {hook.banks}: positive, totalling {POOL_BANKS}, ICAP at most 512 '
                             '(its 18-bit address space is 1 MiB)')
        defines.update(PROFILE_ICAP_BANKS=str(icap), PROFILE_PLE_BANKS=str(ple), PROFILE_PSE_BANKS=str(pse))
    for name, address in hook.calls.items():
        if not ROM_BASE <= address < ROM_BASE + ROM_SIZE or address & 1:
            raise SystemExit(f'call {name}: {address:#010x} is not a halfword-aligned ROM address')
        hi, lo = _split_hi_lo(address)
        defines[f'{name}_HI'], defines[f'{name}_LO'] = f'{hi:#x}', f'{lo:d}'
    return defines


def add_hook(blob, hook):
    """The container with one more redirect entry and its replacement appended."""
    offset, plain = read_container(blob, 'patch')
    body = bytearray(plain)
    bitmap_lo, bitmap_hi = struct.unpack_from('<II', body, 0)
    index = _free_index(bitmap_lo, bitmap_hi)
    code = _assemble(hook.source, _defines(hook))
    copy_at = len(code) - 8 - len(hook.insn)
    if copy_at < 0 or code[copy_at:copy_at + len(hook.insn)] != hook.insn:
        raise SystemExit(f'{hook.source} must end with the copied instruction and the lui/jalr return')
    replacement = code + bytes(-len(code) % 16)

    cut = len(body) - FOOTER_LEN
    to_addr = PATCH_SRAM_BASE + cut
    body[cut:cut] = replacement
    end = PATCH_SRAM_BASE + len(body)
    if end > STATUS_PAGE:
        raise SystemExit(f'the patch copy would reach {end:#x}, over the hook status page {STATUS_PAGE:#x}')

    struct.pack_into('<I', body, 0, bitmap_lo | (1 << index))
    struct.pack_into('<I', body, FROM_OFF + 4 * index, hook.from_addr)
    struct.pack_into('<I', body, TO_OFF + 4 * index, to_addr)

    out = bytearray(blob[:offset])
    out += aes.cbc_zero(bytes(body), encrypt=True)
    struct.pack_into('>I', out, SEC_SIZE, len(body))
    struct.pack_into('>I', out, SEC_LEN, len(body))
    _verify(bytes(out), plain, index, hook.from_addr, to_addr, replacement)
    return bytes(out)


def _verify(built, original, index, from_addr, to_addr, replacement):
    """Decode the output as the device will and check it against the input."""
    _, plain = read_container(built, 'built patch')
    cut = len(original) - FOOTER_LEN
    problems = []
    if plain[TABLE_BYTES:cut] != original[TABLE_BYTES:cut]:
        problems.append('vendor body changed outside the hook table')
    if plain[cut:cut + len(replacement)] != replacement:
        problems.append('replacement is not at the expected offset')
    if plain[-FOOTER_LEN:] != original[-FOOTER_LEN:]:
        problems.append('security footer changed')
    if len(plain) != len(original) + len(replacement):
        problems.append('section length does not account for the replacement')
    lo, hi = struct.unpack_from('<II', plain, 0)
    olo, ohi = struct.unpack_from('<II', original, 0)
    if (lo, hi) != (olo | (1 << index), ohi):
        problems.append(f'bitmap is {lo:#010x}/{hi:#010x}')
    frm, to = struct.unpack_from('<64I', plain, FROM_OFF), struct.unpack_from('<64I', plain, TO_OFF)
    ofrm, oto = struct.unpack_from('<64I', original, FROM_OFF), struct.unpack_from('<64I', original, TO_OFF)
    problems += [f'entry {i} changed' for i in range(N_ENTRY)
                 if i != index and (frm[i], to[i]) != (ofrm[i], oto[i])]
    if (frm[index], to[index]) != (from_addr, to_addr):
        problems.append(f'entry {index} is {frm[index]:#x}->{to[index]:#x}')
    if problems:
        raise SystemExit('the built patch does not verify: ' + '; '.join(problems))


# Our own container: header fields in the vendor format with our values; the ROM sees none of
# them (it receives the section only).
OWN_BUILD_DATE = b'20260101000000\0\0'
OWN_HW_SW_VER = 0x8A108A10           # the chip's hardware/software version word
OWN_SEC_TYPE = 0x00030002            # an INFO section, as the loader requires
OWN_SEC_KEY = 0x01000000             # AES, key 0: the section is encrypted whole


def empty_container():
    """A one-section patch container whose section is an empty hook table and a footer of zeros
    {pubkey 0x40, signature 0x300}: no MediaTek byte in it. add_hook then appends our hooks."""
    plain = bytearray(TABLE_BYTES + (-TABLE_BYTES % 16))
    footer = bytearray(FOOTER_LEN)
    struct.pack_into('<II', footer, 0, 0x40, 0x300)
    plain += footer
    header = bytearray(0xA0)
    header[0:16] = OWN_BUILD_DATE
    header[16:20] = b'ALPS'
    struct.pack_into('>II', header, 20, OWN_HW_SW_VER, 0xFFFFFFFF)
    struct.pack_into('>IIIII', header, 32, 0, 0, 0, 1, 0)
    struct.pack_into('>7I', header, 96, OWN_SEC_TYPE, 0xA0, len(plain), PATCH_SRAM_BASE, len(plain), OWN_SEC_KEY, 0)
    return bytes(header) + aes.cbc_zero(bytes(plain), encrypt=True)


def build_own():
    """Our own patch: only our hooks, no MediaTek body (no vendor redirect, no eFuse recovery)."""
    blob = empty_container()
    for hook in OWN:
        blob = add_hook(blob, hook)
    return blob
