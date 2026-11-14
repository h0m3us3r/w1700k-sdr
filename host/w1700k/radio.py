# SPDX-License-Identifier: MIT
"""The radio as the host sees it: firmware staging and download, the driver, the band
configurations and the calibration EEPROM image.

A load (controller.py) replaces the driver (RESET_MT quiesces DMA first), takes host ownership,
resets the WF subsystem, takes ownership again and downloads our firmware set (patch, WM, WA);
our WM then brings the radio up from cold on command (wm.py). Nothing transmits.
"""
import json
import struct
from dataclasses import dataclass, replace

from .unit import DEVICE, Step, UnitError

FW_DIR = '/tmp/w1700k-fw'         # unit-side copies of the firmware set

RF_BAND = {0: '2g', 1: '5g', 2: '6g'}
RF_BAND_CODE = {'2g': 0, '5g': 1, '6g': 2}
# Rates as (sample rate, capture rate), samples/s -> the bandwidth class our firmware's SETUP /
# RATE take. Every band: one ICAP path of 14-bit IQ at twice the class (20, 40, 80, 160 MHz),
# capture rate = sample rate; the decimation rolls off past ~60 % of Nyquist, signal and noise
# alike (D293). Band 2 also has the 320 MHz class, "640/2": captured at 640 Msps, the stream keeps
# every other sample; 320 Msps flat to about +-155 MHz, but signals 160..320 MHz from the LO fold
# in at -2..-32 dB (D288, D292).
RATE_CODE = {(40_000_000, 40_000_000): 0, (80_000_000, 80_000_000): 1, (160_000_000, 160_000_000): 2,
             (320_000_000, 320_000_000): 3, (320_000_000, 640_000_000): 4}
SAMPLE_RATES = tuple(sorted({sr for sr, _ in RATE_CODE}))
HALF_640_BANDS = (2,)             # bands with the 320 MHz class
# TUNE's reach per band (include/hw/adie/mt7976.h, mt7977.h *_LO_TUNE_*): a little beyond the
# measured VCO limits. Each die's own lock edges lie inside; the controller finds them at load
# (Controller.lo_lock). Any Hz in range, realised on the synthesizer's grid (actual_lo).
LO_RANGE = {0: (2_070_000_000, 2_750_000_000), 1: (4_640_000_000, 8_000_000_000),
            2: (4_640_000_000, 8_000_000_000)}
EEPROM_BYTES, EEPROM_FILE_BYTES = 7680, 16384
# The analog filter is its own setting (the RF command FILTER), independent of the sample rate.
DEFAULT_FILTER_MHZ = 320
# The synthesizer (include/hw/adie/sx.h): LO = f_PFD N num / den, N = INT + FRAC / 2^24 with FRAC
# bit 0 ignored, so the LO moves on a grid of 2 f_PFD / 2^24 x num / den (ADIE_SX_FRAC_STEP_LSB):
# MT7976 5/16 at 80 MHz (2.98 Hz); MT7977 5/8 below 5926 MHz, 3/4 from it, at 160 MHz (11.92 / 14.31 Hz).
MT7977_SPLIT_HZ = 5_926_000_000


def lo_divider(band, hz):
    """(num, den, f_PFD) of the band's synthesizer at LO hz (firmware/radio/rfctl.c rfctl_tune)."""
    if band == 0:
        return 5, 16, 80_000_000
    return (5, 8, 160_000_000) if hz < MT7977_SPLIT_HZ else (3, 4, 160_000_000)


def _round_half_even(n, d):
    q, r = divmod(n, d)
    return q + (1 if 2 * r > d or (2 * r == d and q & 1) else 0)


def actual_lo(band, hz):
    """The LO the synthesizer realises for hz, in Hz rounded to the nearest (rfctl.c divider)."""
    num, den, pfd = lo_divider(band, hz)
    d = num * pfd
    q = ((hz * den << 23) + d // 2) // d << 1        # N in Q24 on the even-FRAC grid
    return _round_half_even(q * d, den << 24)


def lo_step(band, hz):
    """The synthesizer's smallest LO step at hz, Hz."""
    num, den, pfd = lo_divider(band, hz)
    return 2 * pfd * num / den / (1 << 24)


class RadioError(RuntimeError):
    pass


@dataclass(frozen=True)
class BandConfig:
    band: int
    lo_hz: int
    sample_rate: int                      # samples/s, one of SAMPLE_RATES
    capture_rate: int | None = None       # samples/s before the stream; None: the sample rate

    def validate(self):
        """This configuration, the LO as an int; raises ValueError if unsupported."""
        if self.band not in RF_BAND:
            raise ValueError(f'band {self.band}: 0, 1 or 2')
        cfg = replace(self, capture_rate=self.capture_rate or self.sample_rate)
        code = RATE_CODE.get((cfg.sample_rate, cfg.capture_rate))
        if code is None:
            raise ValueError(f'sample rate {", ".join(str(r // 1_000_000) for r in SAMPLE_RATES)} Msps, '
                             'or 320 Msps captured at 640 (640/2)')
        if code == RATE_CODE[(320_000_000, 640_000_000)] and self.band not in HALF_640_BANDS:
            raise ValueError('640/2 is band 2 only')
        lo, hi = LO_RANGE[self.band]
        if not lo <= int(self.lo_hz) <= hi:
            raise ValueError(f'band {self.band} tunes {lo / 1e9:.2f}-{hi / 1e9:.2f} GHz')
        return replace(cfg, lo_hz=int(self.lo_hz))

    @property
    def rf_band(self):
        return RF_BAND[self.band]

    @property
    def rate_code(self):
        return RATE_CODE[(self.sample_rate, self.capture_rate or self.sample_rate)]

    def as_dict(self):
        return {'lo_hz': self.lo_hz, 'sample_rate': self.sample_rate, 'capture_rate': self.capture_rate}


# Initialised on every load; `configure` and `tune` change them.
DEFAULT_BANDS = {
    0: BandConfig(0, 2_442_000_000, 320_000_000).validate(),
    1: BandConfig(1, 5_250_000_000, 320_000_000).validate(),
    2: BandConfig(2, 6_105_000_000, 320_000_000).validate(),
}


# Where a unit carries its calibration: 16 KiB at 0x5000 of the vendor's factory partition, which
# our layout names `calibration` and nothing of ours writes (NAND 0x405000).
UNIT_EEPROM_OFFSET = 0x5000
EEPROM_CHIP_ID = b'\x90\x79'                           # 0x7990, little-endian, where the image starts
UNIT_EEPROM = ('n=$(sed -n \'s/^mtd\\([0-9]*\\):.*"calibration"$/\\1/p\' /proc/mtd); [ -n "$n" ] && '
               f'dd if=/dev/mtd${{n}}ro bs=4096 skip={UNIT_EEPROM_OFFSET // 4096} '
               f'count={EEPROM_FILE_BYTES // 4096} 2>/dev/null')


def unit_eeprom(unit):
    """The calibration EEPROM file the unit itself holds, or None when it gives none (a system
    without the partition, or a partition without an MT7996 image)."""
    result = unit.run(UNIT_EEPROM, timeout=30)
    data = result.stdout
    if result.returncode or len(data) != EEPROM_FILE_BYTES or data[:2] != EEPROM_CHIP_ID:
        return None
    return data


def eeprom_image(data):
    """The 7680 calibration bytes of a unit EEPROM file (16 KiB with a blank tail, or 7680 bytes)."""
    if len(data) == EEPROM_FILE_BYTES:
        if any(b not in (0x00, 0xFF) for b in data[EEPROM_BYTES:]):
            raise RadioError(f'the EEPROM file has data past {EEPROM_BYTES} bytes')
        return data[:EEPROM_BYTES]
    if len(data) != EEPROM_BYTES:
        raise RadioError(f'an EEPROM file is {EEPROM_FILE_BYTES} or {EEPROM_BYTES} bytes, not {len(data)}')
    return data



# --- unit side ---

def _records(result):
    """The JSON lines a client printed."""
    out = []
    for line in result.stdout.decode(errors='replace').splitlines():
        if line.startswith('{'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _failure(result, what):
    if result is None:
        return RadioError(f'{what}: not run')
    records = _records(result)
    last = records[-1] if records else {}
    detail = ', '.join(f'{k} {last[k]}' for k in ('phase_name', 'executor_result', 'errno') if last.get(k))
    text = detail or result.stderr.decode(errors='replace').strip()[-200:] or f'exit {result.exit}'
    return RadioError(f'{what} failed: {text}')


class Radio:
    def __init__(self, unit):
        self.unit = unit

    def stage(self, containers):
        """Copy firmware containers to the unit, skipping files that are already there."""
        listing = self.unit.run(f'sha256sum {FW_DIR}/*.bin 2>/dev/null; true').stdout.decode()
        present = {line.split()[1]: line.split()[0] for line in listing.splitlines() if len(line.split()) == 2}
        for image in containers:
            remote = f'{FW_DIR}/{image.path.name}'
            if present.get(remote) != image.sha256:
                self.unit.push(image.data(), remote)

    def replace_module(self, module):
        """A fresh driver. Any stream is stopped (its pump writes the stop word) and any control
        session closed, since both hold the device; RESET_MT quiesces DMA before the old module
        may go; then insmod. The status LED's program (w1700k-led) opens the device only for the
        instant of each change of the LED, so an rmmod that meets it is tried again."""
        download = self.unit.tool('mt7996-download')
        script = (f'set -e\n'
                  f'n=0\n'
                  f'while pids=$(pidof mt7996-stream); do\n'
                  f'  [ $n -lt 10 ] || {{ echo "a running stream did not stop" >&2; exit 1; }}\n'
                  f'  kill -INT $pids; sleep 1; n=$((n + 1))\n'
                  f'done\n'
                  f'pids=$(pidof mt7996-ctl || true)\n'
                  f'if [ -n "$pids" ]; then kill $pids; sleep 1; fi\n'
                  f'if [ -c {DEVICE} ]; then\n'
                  f'  {download} {DEVICE} --reset >/dev/null\n'
                  f'  n=0\n'
                  f'  until rmmod w1700k_sdr 2>/dev/null; do\n'
                  f'    [ $n -lt 20 ] || {{ rmmod w1700k_sdr; exit 1; }}\n'
                  f'    sleep 0.1; n=$((n + 1))\n'
                  f'  done\n'
                  f'fi\n'
                  f'insmod {module}\n'
                  f'[ -c {DEVICE} ]\n')
        try:
            self.unit.check('sh -s', input=script.encode(), timeout=60)
        except UnitError as exc:
            raise RadioError(f'driver reload failed: {exc}')

    def download(self, containers):
        """Host ownership, RESET_MT, ownership again (the reset revokes it), then each container."""
        probe, download = self.unit.tool('mt7996-probe'), self.unit.tool('mt7996-download')
        acquire = Step((probe, DEVICE, '0x00901000', '4', '--acquire'))
        steps = [acquire, Step((download, DEVICE, '--reset')), acquire]
        steps += [Step((download, DEVICE, f'{FW_DIR}/{c.path.name}', '--i-understand', *c.flags))
                  for c in containers]
        names = ['host ownership', 'RESET_MT', 'host ownership'] + [f'{c.role} download' for c in containers]
        results = self.unit.batch(steps, timeout=240)
        for name, result in zip(names, results):
            if result is None or result.exit:
                raise _failure(result, name)
