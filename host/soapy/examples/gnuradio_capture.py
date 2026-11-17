#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The W1700K in GNU Radio, through GNU Radio's own Soapy source (gr-soapy): a second of the
stream as it is tuned now, its level and its strongest signal.

    SOAPY_SDR_PLUGIN_PATH=host/soapy/build W1700K_CONTROLLER="host/bin/w1700k serve" \\
        python3 host/soapy/examples/gnuradio_capture.py [--freq HZ] [--rate HZ] [--seconds S]

Without --freq and --rate the receiver stays as it is. The samples are kept in memory (16 bytes
each as GNU Radio's complex), so ask for what fits: a second at 320 Msps is 5 GB.
"""
import argparse

import numpy as np
from gnuradio import blocks, gr, soapy


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="driver=w1700k", help="the SoapySDR device (default: %(default)s)")
    parser.add_argument("--freq", type=float, help="tune here first, Hz")
    parser.add_argument("--rate", type=float, help="set this sample rate first: 40e6, 80e6, 160e6 or 320e6")
    parser.add_argument("--seconds", type=float, default=0.05, help="how much to capture (default: %(default)s)")
    args = parser.parse_args()

    top = gr.top_block()
    source = soapy.source(args.device, "fc32", 1, "", "", [""], [""])
    if args.rate:
        source.set_sample_rate(0, args.rate)
    if args.freq:
        source.set_frequency(0, args.freq)
    rate, freq = source.get_sample_rate(0), source.get_frequency(0)
    count = int(args.seconds * rate)
    sink = blocks.vector_sink_c()
    top.connect(source, blocks.head(gr.sizeof_gr_complex, count), sink)
    top.run()

    x = np.array(sink.data())
    size = 1 << min(20, int(np.log2(len(x))))
    spectrum = np.fft.fftshift(np.abs(np.fft.fft(x[:size] * np.hanning(size))) ** 2)
    spectrum[size // 2 - 2:size // 2 + 3] = 0            # the LO's own leakage at the centre
    peak = (int(spectrum.argmax()) - size // 2) * rate / size
    print("%d samples at %.0f Msps around %.3f MHz (antenna %s)" % (len(x), rate / 1e6, freq / 1e6, source.get_antenna(0)))
    print("level %.1f dBFS rms; strongest signal at %.3f MHz" % (
        10 * np.log10(np.mean(np.abs(x) ** 2)), (freq + peak) / 1e6))


if __name__ == "__main__":
    main()
