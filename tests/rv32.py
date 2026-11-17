# SPDX-License-Identifier: MIT
"""A small RV32I interpreter: enough to run the stream firmware's leaf kernels (pack.S,
reduce14.S) from their machine code, with every instruction executed counted."""


class Trap(Exception):
    pass


def _sext(v, bits):
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


class Machine:
    def __init__(self, code, base):
        self.code, self.base = code, base
        self.mem = {}                      # word address -> value: what the code may load and store
        self.x = [0] * 32
        self.executed = 0

    def load(self, address, data):
        assert address % 4 == 0 and len(data) % 4 == 0
        for i in range(0, len(data), 4):
            self.mem[address + i] = int.from_bytes(data[i:i + 4], 'little')

    def dump(self, address, length):
        return b''.join(self.mem[address + i].to_bytes(4, 'little') for i in range(0, length, 4))

    def call(self, entry, *args, limit=2_000_000):
        """Run from `entry` with a0.. = args until it returns; returns a0."""
        x = self.x
        for i, a in enumerate(args):
            x[10 + i] = a & 0xFFFFFFFF
        x[1] = 0xFFFFFFF0                  # ra: the return address that ends the run
        pc = entry
        while pc != 0xFFFFFFF0:
            if self.executed >= limit:
                raise Trap('instruction limit')
            self.executed += 1
            i = int.from_bytes(self.code[pc - self.base:pc - self.base + 4], 'little')
            op, rd, f3, rs1, rs2, f7 = i & 0x7F, i >> 7 & 31, i >> 12 & 7, i >> 15 & 31, i >> 20 & 31, i >> 25
            a, b, nxt = x[rs1], x[rs2], pc + 4
            imm_i = _sext(i >> 20, 12)
            if op == 0x37:                                         # lui
                v = i & 0xFFFFF000
            elif op == 0x13:                                       # op-imm
                sh = rs2
                v = {0: a + imm_i, 1: a << sh, 4: a ^ imm_i, 6: a | imm_i, 7: a & imm_i,
                     5: (_sext(a, 32) >> sh) if f7 & 0x20 else a >> sh}.get(f3)
            elif op == 0x33 and f7 in (0, 0x20):                   # op
                sh = b & 31
                v = {0: a - b if f7 else a + b, 1: a << sh, 4: a ^ b, 6: a | b, 7: a & b,
                     5: (_sext(a, 32) >> sh) if f7 else a >> sh}.get(f3)
            elif op == 0x03 and f3 == 2:                           # lw
                address = (a + imm_i) & 0xFFFFFFFF
                if address not in self.mem:
                    raise Trap(f'load from {address:#x} at {pc:#x}')
                v = self.mem[address]
            elif op == 0x23 and f3 == 2:                           # sw
                address = (a + _sext(f7 << 5 | rd, 12)) & 0xFFFFFFFF
                if address not in self.mem:
                    raise Trap(f'store to {address:#x} at {pc:#x}')
                self.mem[address] = b
                rd, v = 0, 0
            elif op == 0x63:                                       # branches
                off = _sext((i >> 31) << 12 | (i >> 7 & 1) << 11 | (i >> 25 & 0x3F) << 5 | (i >> 8 & 0xF) << 1, 13)
                taken = {0: a == b, 1: a != b, 4: _sext(a, 32) < _sext(b, 32), 5: _sext(a, 32) >= _sext(b, 32),
                         6: a < b, 7: a >= b}.get(f3)
                if taken is None:
                    raise Trap(f'instruction {i:#010x} at {pc:#x}')
                nxt, rd, v = (pc + off if taken else nxt), 0, 0
            elif op == 0x67 and f3 == 0:                           # jalr
                nxt, v = (a + imm_i) & 0xFFFFFFFE, pc + 4
            else:
                v = None
            if v is None:
                raise Trap(f'instruction {i:#010x} at {pc:#x}')
            if rd:
                x[rd] = v & 0xFFFFFFFF
            pc = nxt
        return x[10]
