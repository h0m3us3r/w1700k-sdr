# SPDX-License-Identifier: MIT
"""The reduced slice formats of include/hw/mt7996/iq_reduce.h, written out independently: what
the C encoder and decoder are checked against."""
FRACTION = [round(2 ** (-k / 16) * 65536) for k in range(16)]
M = [max(1, (8192 * FRACTION[c & 15] + (1 << (15 + (c >> 4)))) >> (16 + (c >> 4))) for c in range(209)]
RANGE = {16: 127, 14: 63, 8: 7, 7: 5}
BYTES = {16: 4096, 14: 3584, 8: 2048, 7: 1792}
MAGIC = 0x32524453


def signed14(v):
    v &= 0x3FFF
    return v - 0x4000 if v & 0x2000 else v


def scale(words, fmt):
    """The scale code of a slice: the first multiplier that keeps its peak inside the range."""
    peak = min(max(max(abs(signed14(w >> 16)), abs(signed14(w))) for w in words), 8191)
    return next(c for c in range(209) if peak * M[c] < (2 * RANGE[fmt] + 1) * 4096)


def encode(words, fmt):
    """(scale code, the slice's bytes)."""
    code = scale(words, fmt)
    m = M[code]
    pairs = [((signed14(w >> 16) * m + 4096) >> 13, (signed14(w) * m + 4096) >> 13) for w in words]
    assert all(abs(v) <= RANGE[fmt] for pair in pairs for v in pair)
    out = bytearray()
    if fmt == 16:
        for i, q in pairs:
            out += bytes((q & 0xFF, i & 0xFF))
    elif fmt == 14:
        for g in range(0, len(pairs), 4):
            v = sum(((q & 0x7F) | (i & 0x7F) << 7) << (14 * j) for j, (i, q) in enumerate(pairs[g:g + 4]))
            out += v.to_bytes(7, 'little')
    elif fmt == 8:
        out += bytes((i & 15) << 4 | (q & 15) for i, q in pairs)
    else:
        for g in range(0, len(pairs), 8):
            v = sum(((i + 5) * 11 + q + 5) << (7 * j) for j, (i, q) in enumerate(pairs[g:g + 8]))
            out += v.to_bytes(7, 'little')
    assert len(out) == BYTES[fmt]
    return code, bytes(out)


def restored(words, fmt):
    """The (I, Q) a receiver holds for the slice."""
    code = scale(words, fmt)
    m = M[code]
    step = (8192 * 1024 + m // 2) // m

    def back(x):
        return min(max((((x * m + 4096) >> 13) * step + 512) >> 10, -8192), 8191)
    return [(back(signed14(w >> 16)), back(signed14(w))) for w in words]


def step_of(words, fmt):
    return 8192 / M[scale(words, fmt)]


def header_word(seq, fmt, code):
    return seq | fmt << 48 | code << 56
