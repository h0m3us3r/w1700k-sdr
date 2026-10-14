# Copy received samples into DSP memory with MEM_DMA

The change is in [`wm-dma.c`](wm-dma.c), function `dma_trial()`: it submits
source/destination descriptors to MEM_DMA instead of copying IQ words on the WM
CPU. The measured 8 KiB transfers take **7–9 µs**. The earlier WM scalar-copy
baseline took **352 µs** for two capture-window pages totaling 8 KiB.
This is a callable transfer helper and test harness; the default receiver does
not use it yet.

```text
Stopped ICAP pool/window -> MEM_DMA TX0 -> RX0 -> DSP0 DTCM
                           WM submits descriptors; no CPU payload copy
```

## Review map

| File | Purpose |
| --- | --- |
| `wm-dma.c` | The DMA implementation: rings, submission, completion and shutdown |
| `dma_bench.py`, `capture.py` | Check known patterns and received samples against destination bytes and guards |
| `start.S`, `build.py`, `load.py` | Boot an idle DSP through the existing PDA downloader so its DTCM is accessible |
| `common.py` | Shared host access and readiness checks |

The DSP test image only initializes the core, reports ready, and spins. It has
no FFT implementation. The two hardware-header edits document the discovered
path and its completion requirements.

## Hardware check

W1700K2 / MT7996, 2026-10-14, using the idle DSP image in this directory:

| Source → DSP0, 8 KiB total | DMA time |
| --- | --- |
| Repeated known pattern (ULM1) | 8.80 µs; 912 MB/s including queue control |
| Stopped ICAP pool | 7.20 µs (5,181 WM cycles) |
| Two stopped ICAP window pages | 7.92 µs (5,703 WM cycles) |

All 1,280 pattern transfers completed without descriptor/IRQ errors. Full payload
and guards matched after every batch and for both received captures. These are
transfer timings at an assumed WM clock of 720 MHz, excluding setup and host
validation reads. The scalar-copy number above is a prior measurement, not a
same-run comparison.

## Transfer and ownership

`dma_trial(source, destination, bytes, repetitions)` uses MEM_DMA at `0x58000000`
with eight-entry TX0/RX0 rings. It preserves the ROM prefetch allocation, publishes
receive capacity before TX, and fences descriptor writes. Completion requires
matching TX/RX DIDX **and RX DDONE**: DIDX was observed advancing before DDONE.
After checking length it recycles the receive slot. Only one transfer is pending
at a time; polling and shutdown have bounded timeouts.

| MCU address | Reservation |
| --- | --- |
| `0x00910000..0x009107ff` | WM helper code |
| `0x00910800`, `0x00910900`, `0x00910a00` | TX ring, RX ring, result counters |
| `0x00912000` | Known-pattern source |
| `0x00464000`, `0x00466000` | Stopped capture-window pages |
| `0xe0400000` | Stopped capture-pool source |
| `0x8620d000..0x8620efff` | DSP0 destination |

The caller owns idle MEM_DMA and these reservations. The helper leaves queues
stopped; reload normal firmware before another receiver uses them. Validation
reads are outside the timing; the helper invalidates clean WM source-cache lines
for pool readback. Tests use stopped captures or repeated static buffers, so they
do not establish continuous capture, concurrent host streaming, or FFT throughput.

## Reproduce

Requires an installed upstream SDR system, its working own firmware and config,
Python 3.10+, LLVM `clang` with RISC-V support, `ld.lld`, `llvm-objcopy`, and
`openssl`. Stop any service that could restart capture. Run from the repo root:

```sh
python3 tools/dsp/build.py
export PYTHONPATH="$PWD/host"
CONFIG="$HOME/.config/w1700k/config.json"
python3 host/bin/w1700k --config "$CONFIG" stop
python3 tools/dsp/load.py --config "$CONFIG" \
  --container tools/dsp/build/w1700k_dma_test.bin
python3 host/bin/w1700k --config "$CONFIG" band 0 --rate 40
python3 tools/dsp/dma_bench.py --config "$CONFIG" \
  --helper tools/dsp/build/wm-dma.bin --out tools/dsp/build/bench.json
python3 tools/dsp/capture.py --config "$CONFIG" --pool \
  --dma-helper tools/dsp/build/wm-dma.bin --out tools/dsp/build/pool.json
python3 tools/dsp/capture.py --config "$CONFIG" \
  --dma-helper tools/dsp/build/wm-dma.bin --out tools/dsp/build/window.json
# Restore normal firmware, including after a failed test:
python3 host/bin/w1700k --config "$CONFIG" load --band 0
```

The benchmark checks full contents and both guards after each 128-transfer run:
five sizes (64–8192 bytes), two patterns, 1,280 transfers total. The capture tests
compare every received word and both destination guards; they do not save RF data.
Timing conversions assume WM at 720 MHz.

Everything is loaded into RAM. Use the normal downloader: directly accessing TCM
while DSP reset is held previously hung the radio bus and required a power cycle.
Reload before replacing a helper at an already-executed address, because of WM
instruction caching.
