# SPDX-License-Identifier: MIT
"""A simulated unit behind the ConsoleSession interface, for testing init/ offline.

It models the console states (stock U-Boot, the dumper, our loader, the
restorer, Linux), RAM, the loader partition, the stock environment, the UBI
volumes and the vendor-logical NAND data well enough that
installation, update and restore runs can execute end to end, and it records
every command it was given. Images are recognised by a marker after the FIT
magic.
"""
import hashlib
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'init'))
from initkit.config import partitions  # noqa: E402
from initkit.uart import MUTATING  # noqa: E402
import init_fake_loadb  # noqa: E402

PROMPTS = {'stock': 'ECNT> ', 'dumper': 'dumper> ', 'loader': 'SDR loader> ',
           'restorer': 'restore> ', 'linux': '/ # '}
STOCK_BOOTCMD = 'flash imgread 2048;bootm'
# The radio calibration image in the calibration partition: chip ID 0x7990 first.
CALIBRATION = b'\x90\x79' + bytes(7678)
IDENTITY = (b'wan_mac=00:11:22:33:44:55\nserial_number=W1700KTEST0001\n'
            b'manufacturer=Gemtek\nwifi_passphrase=secret\n')
RC = re.compile(r'^(.*); echo (W1700K_RC_[0-9a-f]+=)\$\?$', re.S)


def split_commands(line):
    """Split a command line at `; ` outside single quotes, as U-Boot's shell does."""
    parts, current, quoted = [], '', False
    for index, char in enumerate(line):
        if char == "'":
            quoted = not quoted
        if not quoted and line.startswith('; ', index):
            parts.append(current)
            current = ''
            continue
        if not (current == '' and char == ' ' and parts):
            current += char
    return parts + [current]


def fit(marker, size=0x9000):
    """A fake FIT: the magic, a marker naming what it is, then filler."""
    body = b'\xd0\x0d\xfe\xed' + marker.encode() + b'\0'
    return body + bytes((index * 7) & 0xff for index in range(size - len(body)))


class Refused(Exception):
    """The simulated console refused a write, as the real one does."""


class FakeLine:
    """The console as `loadb` has it: what is written goes to U-Boot's receiver,
    and when the transfer ends the image is in RAM and the prompt is back."""

    def __init__(self, unit):
        self.unit = unit
        self.receiver = init_fake_loadb.Receiver()
        self.replies = []

    def write(self, data):
        unit = self.unit
        if unit.loading is None or unit.line == 'dead':
            return
        reply = self.receiver.feed(data)
        if reply:
            self.replies.append(reply)
        if self.receiver.done:
            data = bytes(self.receiver.memory)
            if unit.line == 'corrupting' and data:      # past Kermit's six-bit check
                data = data[:100] + bytes((data[100] ^ 0x20,)) + data[101:]
            size = len(data) - (1 if unit.line == 'short' else 0)
            unit.write_ram(unit.loading, data)
            unit.buffer += (f'## Total Size      = {size:#010x} = {size} Bytes\r\n'
                            f'## Start Addr      = {unit.loading:#010x}\r\n' + PROMPTS['stock'])
            unit.loading = None

    def read(self, timeout):
        return self.replies.pop(0) if self.replies else b''


class FakeUnit:
    def __init__(self, served, chip_bytes=0x420000, installed=False,
                 translation='vendor BBT/BMT', part=partitions.KV, stock_pool=None,
                 loadb=True, line='clean'):
        self.served = Path(served)
        # Stock's `loadb`: whether it has the command, the address a transfer is
        # going to, and what the serial line does to one ('clean', 'dead',
        # 'corrupting', 'short').
        self.loadb = loadb
        self.loading = None
        self.line = line
        self.part = part
        # The vendor pool our U-Boots compute, and the one stock prints at boot.
        self.pool = min(250, part.blocks * 8 // 100)
        self.stock_pool = stock_pool or self.pool
        self.probed = False            # our U-Boots name the part at the first probe
        self.detached = False          # the dumper's bmtdetach
        self.state = 'off'
        self.env = {'bootcmd': STOCK_BOOTCMD, 'ethaddr': '00:aa:bb:01:23:40'}
        self.saved = dict(self.env)
        self.ram = {}
        self.loader_slot = b''
        self.ubi = {'system': {}, 'durable': {}}
        self.attached = None
        self.erased = []
        self.created = {}              # partition: [(volume, size, type, id)]
        self.log = []                  # (state, command)
        self.chip_bytes = chip_bytes
        self.translation = translation
        self.allow_mutating = False
        self.buffer = ''
        # The vendor-logical NAND data: the dumper reads it, the restorer rewrites it.
        self.nand = bytearray(b'\xff' * chip_bytes)
        self.nand[0x400000:0x400000 + len(IDENTITY)] = IDENTITY
        self.nand[0x405000:0x405000 + len(CALIBRATION)] = CALIBRATION
        self.nand_writes = []          # (offset, length)
        self.catcher_armed = True
        if installed:
            self.loader_slot = fit('loader') + b'\xff' * (0x40000 - 0x9000)
            self.saved['bootcmd'] = self.env['bootcmd'] = \
                'flash read 0x600000 0x40000 0x89000000; bootm 0x89000000'
            self.ubi['system'] = {'ubootenv': b'', 'ubootenv2': b'', 'fit': fit('system'),
                                  'rootfs': b'hsqs-old'}

    # -- the ConsoleSession interface ----------------------------------------
    def check_command(self, line):
        if MUTATING.search(line) and not self.allow_mutating:
            raise Refused(line)

    def mark(self):
        return len(self.buffer)

    def view(self, start=0):
        return self.buffer[start:]

    def at_prompt(self, which):
        return self.state == which

    def wait(self, ready, timeout, description):
        if self.state == 'loader-countdown':        # nobody interrupted: autoboot
            self.state = 'loader'
            self.run_bootcmd()
        elif self.state == 'rebooting' and not self.catcher_armed:
            self.boot_unattended()
        return bool(ready())

    def arm(self):
        self.catcher_armed = True

    def wait_for_catch(self, timeout):
        if not self.catcher_armed:
            raise RuntimeError('no catcher armed')
        self.catcher_armed = False
        self.boot_stock()
        return 'confirmed'

    def expect_prompt(self, which, timeout=None):
        if self.state != which:
            raise RuntimeError(f'expected {which}, at {self.state}')
        return which

    def interrupt_autoboot(self, which, limit=None):
        if self.state == 'loader-countdown':
            self.state = 'loader'
        return self.expect_prompt(which)

    def wake(self):
        self.log.append((self.state, '<ctrl-c>'))
        self.loading = None                 # Ctrl-C ends loadb's wait

    def binary(self):
        return FakeLine(self)

    def send_async(self, line):
        self.check_command(line)
        self.log.append((self.state, line))
        if line in ('reset', 'reboot -f'):
            self.state = 'rebooting'
        elif line.startswith('bootm '):
            self.bootm(int(line.split()[1].split('#')[0], 16), line)
        elif line == 'run bootcmd':
            self.run_bootcmd()
        elif line.startswith('loadb ') and self.state == 'stock':
            if self.loadb:
                self.loading = int(line.split()[1], 16)
                self.buffer += (f'{line}\r\n## Ready for binary (kermit) download to '
                                f'{self.loading:#010x} at 115200 bps...\r\n')
            else:
                self.buffer += f"{line}\r\nUnknown command 'loadb' - try 'help'\r\nECNT> "
        else:
            raise AssertionError(f'unexpected async command {line!r}')

    def must_run(self, line, limit=None, heartbeat=None, about=None):
        self.check_command(line)
        match = RC.match(line)
        base, token = (match.group(1), match.group(2)) if match else (line, None)
        self.log.append((self.state, base))
        output, status = self.execute(base)
        text = f'{line}\n{output}\n'
        if token:
            text += f'{token}{status}\n'
        text += PROMPTS[self.state]
        self.buffer += text
        return text

    def run(self, line, **options):
        return self.must_run(line, **options), True

    # -- boot transitions ----------------------------------------------------
    def boot_stock(self):
        self.env = dict(self.saved)
        self.ram = {}
        self.attached = None
        self.probed = self.detached = False
        self.buffer += f'bmt pool size: {self.stock_pool} \r\nBMT & BBT Init Success \r\n'
        self.state = 'stock'

    def bootm(self, address, line):
        image = self.ram.get(address, b'')
        if b'dumper' in image[:16]:
            self.state = 'dumper'
        elif b'restorer' in image[:16]:
            assert self.state == 'stock', 'stock U-Boot boots the restorer'
            self.state = 'restorer'
        elif b'loader' in image[:16]:
            self.state = 'loader-countdown'
        elif b'recovery' in image[:16] and line.endswith('#conf-sdr'):
            self.state = 'linux'
            self.buffer += 'W1700K_BOOT_READY\n/ # '
        else:
            raise AssertionError(f'bootm of an unknown image at {address:#x}')

    def boot_unattended(self):
        """Stock U-Boot boots on its own: our loader, or the stock kernel."""
        self.boot_stock()
        if self.saved['bootcmd'] == STOCK_BOOTCMD:
            self.buffer += 'U-Boot 2014.04-rc1\nStarting kernel ...\n'
            self.state = 'stock-linux'
        else:
            self.run_bootcmd()
            self.buffer += 'U-Boot 2026.07\nHit any key to stop autoboot:  5 '

    def run_bootcmd(self):
        if self.state == 'stock':
            match = re.match(r'flash read 0x600000 (0x[0-9a-f]+) 0x89000000; bootm 0x89000000$',
                             self.env['bootcmd'])
            assert match, f'stock bootcmd {self.env["bootcmd"]!r} does not boot our loader'
            self.ram[0x89000000] = self.loader_slot[:int(match.group(1), 16)]
            self.bootm(0x89000000, 'bootm')
        elif self.state == 'loader':
            assert b'system' in self.ubi['system'].get('fit', b'')[:16], 'no system FIT'
            self.state = 'linux'
            self.buffer += 'W1700K_BOOT_READY\n/ # '
        else:
            raise AssertionError(f'run bootcmd at {self.state}')

    # -- commands --------------------------------------------------------------
    def execute(self, line):
        parts = split_commands(line)
        if len(parts) > 1:
            for part in parts:
                self.execute(part)
            return '', 0
        words = line.split()
        name = re.sub(r'[./-]', '_', words[0])
        handler = getattr(self, f'{self.state}_{name}', None) or \
            getattr(self, f'any_{words[0].split(".")[0]}', None)
        if handler is None:
            raise AssertionError(f'{self.state} does not know {line!r}')
        return handler(line, words)

    def any_setenv(self, line, words):
        name, _, value = line[len('setenv '):].partition(' ')
        self.env[name] = value.strip("'")
        return '', 0

    def any_printenv(self, line, words):
        return '\n'.join(f'{key}={value}' for key, value in self.env.items()), 0

    def any_tftpboot(self, line, words):
        path = self.served / words[2]
        if not path.is_file():
            return 'TFTP error: File not found', 1
        data = path.read_bytes()
        self.write_ram(int(words[1], 16), data)
        return f'Bytes transferred = {len(data)} ({len(data):x} hex)', 0

    def write_ram(self, address, data):
        """Store `data` at `address`, inside an existing piece when it falls in one."""
        for start, piece in self.ram.items():
            if start <= address and address + len(data) <= start + len(piece) and \
                    (start < address or len(data) < len(piece)):
                at = address - start
                self.ram[start] = piece[:at] + data + piece[at + len(data):]
                return
        self.ram[address] = data

    def any_unzip(self, line, words):
        source, target, limit = (int(word, 16) for word in words[1:4])
        # Like U-Boot's gunzip, stop at the end of the stream; RAM after it is
        # whatever an earlier, longer load left there.
        data = zlib.decompressobj(31).decompress(self.ram[source])[:limit]
        self.write_ram(target, data)
        return f'Uncompressed size: {len(data)} = 0x{len(data):X}', 0

    def read_ram(self, address, length):
        """RAM as one span: pieces loaded at consecutive addresses join up, and a
        read may start inside a piece."""
        inside = [start for start, piece in self.ram.items()
                  if start < address < start + len(piece)]
        if inside:
            start = inside[0]
            return (self.ram[start][address - start:] +
                    self.read_ram(start + len(self.ram[start]), 0x7fffffff))[:length]
        data = bytearray()
        cursor = address
        while len(data) < length and cursor in self.ram:
            piece = self.ram[cursor]
            data += piece
            cursor += len(piece)
        return bytes(data[:length])

    def any_crc32(self, line, words):
        data = self.read_ram(int(words[1], 16), int(words[2], 16))
        return f'CRC32 for {words[1]} ==> {zlib.crc32(data):08x}', 0

    def any_iminfo(self, line, words):
        data = self.read_ram(int(words[1], 16), 16)
        if not data.startswith(b'\xd0\x0d\xfe\xed'):
            return 'Bad Magic Number', 1
        # FITs stock can boot (ours for it, and the restorer) carry SHA-1.
        algorithm = 'sha1' if self.state == 'stock' or b'restorer' in data else 'sha256'
        return f'   Hash(es) for Image 0 (kernel): {algorithm}+\n' \
               f'   Hash(es) for Image 1 (fdt): {algorithm}+', 0

    def any_hash(self, line, words):
        data = self.read_ram(int(words[2], 16), int(words[3], 16))
        return f'sha256 for {words[2]} ==> {hashlib.sha256(data).hexdigest()}', 0

    def stock_flash(self, line, words):
        action, offset, length = words[1], int(words[2], 16), int(words[3], 16)
        assert offset == 0x600000, 'stock wrote outside the loader slot'
        assert not length % self.part.block, 'stock was given a part of a block'
        if action == 'erase':
            self.loader_slot = b'\xff' * length
        elif action == 'write':
            self.loader_slot = self.read_ram(int(words[4], 16), length)
        elif action == 'read':
            self.ram[int(words[4], 16)] = self.loader_slot[:length]
        return f'{action}: addr={offset:#x}', 0

    def stock_cmp_b(self, line, words):
        a = self.read_ram(int(words[1], 16), int(words[3], 16))
        b = self.read_ram(int(words[2], 16), int(words[3], 16))
        return ('Total of bytes were the same', 0) if a == b else ('differ', 1)

    def stock_saveenv(self, line, words):
        self.saved = dict(self.env)
        return 'Saving Environment to ECNT Flash...', 0

    def probe_line(self):
        """shared U-Boot patch 0006, printed once per boot at the first probe."""
        if self.probed:
            return ''
        self.probed = True
        part = self.part
        line = (f'spi-nand: {part.id.hex(" ")}, 512 MiB, {part.block >> 10} KiB blocks of '
                f'64 x ({part.page} + {part.oob}) bytes\n')
        if self.translation != 'none':
            line += (f'airoha-bmt: attached (system_blocks={part.blocks - self.pool}, '
                     f'pool={self.pool}, bbt=0, bmt=0); markbad refused\n')
        return line

    def geometry(self):
        return (f'  - block size: {self.part.block:#x} bytes\n'
                f'  - min I/O: {self.part.page:#x} bytes\n'
                f'  - OOB size: {self.part.oob} bytes\n')

    def raw_image(self, size):
        """What `mtd read.raw.oob` returns: every main area, then every OOB."""
        pages = size // self.part.page
        main = bytes((index * 31 + 7) & 0xff for index in range(size))
        return main + bytes((index * 3 + 1) & 0xff for index in range(pages * self.part.oob))

    def dumper_mtd(self, line, words):
        if words[1] == 'list':
            translation = 'none' if self.detached else self.translation
            return (f'{self.probe_line()}* spi-nand0\n  - flags: 0x0 (read-only)\n'
                    f'  - translation: {translation}\n{self.geometry()}'
                    f'  - 0x000000000000-0x{self.chip_bytes:012x} : "spi-nand0"'), 0
        if words[1] == 'read.raw.oob.dontskipbad':
            assert self.detached or self.translation == 'none', 'a raw dump of a translated chip'
            offset, length = int(words[4], 16), int(words[5], 16)
            assert offset == 0 and length == self.chip_bytes, line
            self.ram[int(words[3], 16)] = self.raw_image(length)
            return f'Reading {length:#x} byte(s) [raw] [oob]', 0
        if words[1] == 'read.dontskipbad':
            offset, length = int(words[4], 16), int(words[5], 16)
            self.ram[int(words[3], 16)] = bytes(self.nand[offset:offset + length])
            return 'Reading', 0
        raise AssertionError(line)

    def dumper_bmtdetach(self, line, words):
        if self.translation == 'none' or self.detached:
            return 'airoha-bmt: not attached; the chip is already addressed physically', 0
        self.detached = True
        return 'airoha-bmt: detached; spi-nand0 is addressed physically', 0

    def dumper_md_b(self, line, words):
        address, length = int(words[1], 16), int(words[2], 16)
        base = max(start for start in self.ram if start <= address)
        data = self.ram[base][address - base:address - base + length]
        lines = []
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            lines.append(f'{address + offset:08x}:' + ''.join(f' {c:02x}' for c in chunk) + '  x')
        return '\n'.join(lines), 0

    def dumper_tftpput(self, line, words):
        data = self.read_ram(int(words[1], 16), int(words[2], 16))
        (self.served / words[3]).write_bytes(data)
        return f'Bytes transferred = {len(data)}', 0

    def restorer_mtd(self, line, words):
        if words[1] == 'list':
            return (f'{self.probe_line()}* spi-nand0\n  - flags: 0x400 (writeable)\n'
                    f'  - translation: {self.translation}\n{self.geometry()}'
                    f'  - 0x000000000000-0x{len(self.nand):012x} : "spi-nand0"'), 0
        if words[1] == 'read.dontskipbad':
            offset, length = int(words[4], 16), int(words[5], 16)
            self.ram[int(words[3], 16)] = bytes(self.nand[offset:offset + length])
            return 'Reading', 0
        raise AssertionError(line)

    def restorer_nandsync(self, line, words, check=False):
        address, offset, length = (int(word, 16) for word in words[2:5])
        want = self.read_ram(address, length)
        assert len(want) == length, 'nandsync past the loaded RAM'
        differ, size = 0, self.part.block
        assert not offset % size and not length % size, 'nandsync off the block grid'
        for at in range(0, length, size):
            block = want[at:at + size]
            if self.nand[offset + at:offset + at + size] == block:
                continue
            differ += 1
            if not check:
                self.nand[offset + at:offset + at + size] = block
                self.nand_writes.append((offset + at, size))
                if offset + at == 0x200000:
                    match = re.search(rb'bootcmd=([^\n\0]*)', block)
                    if match:
                        self.saved['bootcmd'] = match.group(1).decode()
        word = 'differ' if check else 'rewritten and read back'
        return f'nandsync: {length // size} blocks, {differ} {word}', 0

    def restorer_nandsync_check(self, line, words):
        return self.restorer_nandsync(line, words, check=True)

    def restorer_md_b(self, line, words):
        return self.dumper_md_b(line, words)

    def restorer_bmtstats(self, line, words):
        return 'bmtstats erase=0/0 program=0/0', 0

    def loader_ubi(self, line, words):
        action = words[1]
        if action == 'detach':
            self.attached = None
            return '', 0
        if action == 'part.rw':
            self.attached = words[2]
            return f"UBI partition '{words[2]}' selected read-write", 0
        volumes = self.ubi[self.attached]
        if action == 'create':
            volumes[words[2]] = b''
            self.created.setdefault(self.attached, []).append(tuple(words[2:]))
            return f'Creating volume {words[2]}', 0
        if action == 'write':
            volumes[words[3]] = self.read_ram(int(words[2], 16), int(words[4], 16))
            return f'{int(words[4], 16)} bytes written to volume {words[3]}', 0
        if action == 'read':
            data = volumes.get(words[3], b'')[:int(words[4], 16)]
            self.ram[int(words[2], 16)] = data
            return f'Read {len(data)} bytes from volume {words[3]}', 0
        raise AssertionError(line)

    def loader_mtd(self, line, words):
        if words[1] == 'list':
            return (f'* spi-nand0\n{self.geometry()}'
                    f'  - 0x000000000000-0x{self.chip_bytes:012x} : "spi-nand0"'), 0
        assert words[1] == 'erase', line
        self.erased.append((words[2], int(words[3], 16), int(words[4], 16)))
        self.ubi[words[2]] = {}
        return 'Erasing', 0

    def linux__usr_sbin_verify_storage(self, line, words):
        return 'W1700K_STORAGE_READY', 0
