#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Load a DMA test DSP container through PDA and perform normal RX bring-up.

Viewer/capture must be stopped first. All changes are volatile. Restoring the
normal firmware set before restarting the viewer removes the test program.
"""
import argparse
import hashlib
from pathlib import Path
from w1700k import config, firmware, rf
from w1700k.controller import Controller
from w1700k.unit import Unit
from w1700k.wm import Wm
from common import emit, healthy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--container', type=Path, required=True)
    args = parser.parse_args()
    cfg = config.load(args.config)
    unit = Unit(cfg.unit)
    with Wm(unit) as w:
        healthy(w)
        if w.session.streaming():
            raise RuntimeError('stop the viewer and stream first')
    data = args.container.read_bytes()
    dsp = firmware.Image('dsp', args.container, len(data), hashlib.sha256(data).hexdigest(), ('--dsp',))
    base = firmware.load(cfg.firmware_dir).containers
    images = base[:2] + (dsp,) + base[2:]
    emit(event='load_research_firmware', dsp_bytes=len(data), dsp_sha256=dsp.sha256)
    # Scope the experimental fourth container to this process. The production
    # manifest and its strict three-role validation remain unchanged.
    original = firmware.load
    controller = Controller(cfg, emit=lambda event: emit(**event), unit=unit)
    firmware.load = lambda directory: firmware.Firmware(images)
    try:
        controller.attach()
        controller.load(0)
    finally:
        controller._close_ctl()
        firmware.load = original
    with Wm(unit) as w:
        healthy(w)
        for address, expected in ((0x81020010, 7), (0x81020014, 0),
                                  (0x86c00010, 1), (0x86c00014, 0x44535031),
                                  (0x86200000, 0x44535031), (0x86200028, 1)):
            emit(event='before_read', address=hex(address))
            value = w.run([rf.r32(address)])[0]
            healthy(w)
            emit(event='read', address=hex(address), value=hex(value))
            if value != expected:
                raise RuntimeError('DSP startup check failed; do not access further DSP memory')
    emit(event='dsp_started', band=0)


if __name__ == '__main__':
    main()
