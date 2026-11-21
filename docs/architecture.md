# Architecture

The W1700K is a Wi-Fi 7 router: an Airoha AN7581 SoC (four Cortex-A53 cores,
2 GiB DRAM, 10 GbE) with a MediaTek MT7996 radio on PCIe. The MT7996 has three
RF front ends (A-dies): an MT7976 for 2.4 GHz and two MT7977s for 5 and 6 GHz.
This project turns it into a wideband receiver that streams raw 14-bit IQ at up
to 320 Msps to a host over 10 GbE, with a browser spectrum/waterfall and live
receiver controls. The MT7996 runs only our firmware.

## Components

| Where it runs | Component | Role |
| --- | --- | --- |
| AN7581 NAND | U-Boot loader | boots the system FIT from UBI; recovery prompt |
| AN7581 | Linux kernel and root filesystem | minimal musl/BusyBox system; 10 GbE |
| AN7581 | `w1700k_sdr` kernel module | owns the MT7996: firmware download, the DRAM stream ring, the zero-copy UDP export, the firmware's command mailbox |
| AN7581 | `mt7996-*` clients | small static programs the host drives over ssh |
| MT7996 ROM patch | our hooks | firmware loading, the SYSRAM pool layout (1 MiB for capture), a start without MediaTek's DSP image |
| MT7996 DSP cores | our occupancy program | core 0 takes the spectrum of chunks of the stream and keeps account of what is in the air |
| MT7996 WM MCU | our WM | brings the radio up from cold; sets each band's LO, sample rate, filter and capture path on command; runs the stream producer |
| MT7996 WA MCU | our WA | packs its share of chunks to 28 bits per sample pair |
| Host (x86 Linux) | `w1700k` | load, configure, stream, RF control: commands to our firmware |
| Host | `w1700k-stream` | receives the export; serves the IQ stream to every consumer through shared memory, unpacked, gap-tagged, optionally corrected for the decimation's roll-off; checks the export's integrity |
| Host | `w1700k-spectrum` | FFT and web UI over the served stream; drives the unit through `w1700k serve` |
| Host | `sdr-recv` | records the served stream |
| Host | `libw1700kSupport.so` | the SoapySDR module (`host/soapy`): the served stream and the controller as a SoapySDR device |

## Bring-up

The host downloads our ROM patch, WM and WA, writes the unit's calibration
EEPROM into our WM, and sends commands; our WM does the rest:

1. **BRINGUP**, from a cold radio: what MediaTek's ROM patch does at download
   (XO, pin mux, the CONN AFE calibration pulse per band, the A-die TOP init
   by chip ID, pads and CONN switch, the A-dies' eFuse trims); the UMAC pool
   layout; the common and per-band PHY initialisation; on each A-die the RF
   init tables, the resistor calibration and the RC filter calibration; the
   FEM outputs and the crystal trim.
2. **FILTER** and **SETUP** per band: the analog filter, then everything
   MediaTek's receive-only channel set wrote, computed from the band's LO (Hz)
   and bandwidth class (the sample rate) alone (per-frequency-group A-die
   tables, PHY tables, RX front end, the D-die RX setup, the RX gain offsets),
   the synthesizer by the TUNE program, then the RX DC-offset calibration at
   that LO and filter. No channel numbers anywhere.
3. **CAPTURE** per band: the capture path (DFE capture mux, trigger block,
   clocks) and the MAC receive enable; **SELECT** points the capture engine at
   the band to stream.

About 2 s from a cold radio. Every procedure is MediaTek's, decoded against a
trace of every register access of MediaTek's firmware on a power-cycled unit,
reduced to what reception needs, and written from the register headers
(`include/hw/`). The transmit calibrations, TX power control and the TTG IQ
calibration (whose filters sit after the capture tap) are left out; the MAC's
transmit path stays disabled.

## Data path

```
 A-die (LNA, mixer, LO, analog filter)
   │ analog IQ
 MT7996 PHY ── DFE / lane mux ── ICAP0 capture engine
   │ 16-byte beats, 14-bit I/Q, ring over 1 MiB of the SYSRAM pool
 WM producer: follows the capture pointer (ICAPCR9), publishes 8 KiB chunks
   │ (WM and WA pack half the chunks to 7 KiB each: 28 bits per I/Q pair)
 MCU DMA TX0 ──PCIe──▶ host WFDMA0 RX0 (1536 descriptors)
   │
 AN7581 DRAM: one 1 GiB ring of 131,072 × 8 KiB slices, written by DMA,
   │          mapped read-only into userspace, never copied
 kernel export: one UDP datagram per slice, pages attached (zero copy),
   │            sent on the 10 GbE port at line rate
 host: w1700k-stream (8 receive threads → reorder ring → workers: unpack,
   │    optional correction) → shared ring of complex int16
   └──→ consumers: w1700k-spectrum (FFT workers → browser), sdr-recv, SoapySDR programs (host/soapy), …
```

Rates. The sample rate is twice the radio's bandwidth class (20, 40, 80 or
160 MHz): 40 to 320 Msps on every band; at 160 MHz the radio gives two 320 Msps
paths, and the stream carries one. Each class's decimation rolls off past about
60 % of Nyquist (-19 dB at 94 %), signal and noise alike, so the edges lose
level, not SNR. Band 2 also has the 320 MHz class: one 640 Msps path on its own
IP's wide datapath, of which the capture keeps every other sample ("640/2",
320 Msps): flat to about ±155 MHz, but signals 160–320 MHz from the LO fold in
at -2 to -32 dB. The other bands have no wide datapath (each A-die is wired to
its band) and deliver no data at that class; band 2 tunes 4.7–7.6 GHz, so 640/2
covers band 1's range too. Band 0 above 40 MHz is our set-up, MediaTek's 2.4 GHz tables ending there. The
LO and the analog filter are set apart from the rate, on every band.

Throughput. 320 Msps × 32 bits is 1.28 GB/s from the radio. One 10 GbE link
carries about 1.24 GB/s of payload, so half of the chunks are packed on the
MT7996 (8 KiB → 7 KiB), which puts the link at about 97 %. The 1 GiB ring
absorbs short stalls (about 0.8 s at full rate).

Slower links. The packing follows the speed the unit's port negotiates. Every
chunk packed carries 160 Msps on 5 Gbit/s, lossless and still zero-copy. Below
what the lossless packings fit, samples are requantised to fewer bits, and
where that happens is set by what each part can do: the SoC's memory bus
carries about 1.0 GB/s of reads beside the capture, so up to 160 Msps the SoC
reduces the stream alone (two threads of `mt7996-stream`, NEON, each on a CPU
of its own, sending through sockets); at 320 Msps neither the SoC nor the
radio's two cores can touch every sample, so the radio reduces 10 of every 16
chunks itself (they still leave zero-copy) and the SoC the 6 between: 14 bits
per sample pair, 4.6 Gbit/s, on a 5 Gbit/s link. The host puts both back as
14-bit samples in one stream; see the top-level README's table.

## Control path

The host talks to our WM through words in its mailbox page in patch SRAM,
which the driver writes and reads (`W1700K_SDR_CTL`); during a stream the
driver's pump performs them between sweeps. Two mechanisms share it:

- **Radio commands** (BRINGUP, SETUP, RATE, DCOC, CAPTURE, SELECT, STREAM) in our WM's
  status block, each acknowledged with a result, the first hardware failure
  and its duration. A STREAM command runs the producer and is acknowledged
  when the stream ends.
- **RF commands** in the control mailbox: tune, gain, filter, RC trim, status,
  lock state and band switch. Our WM builds each command's register program
  and its interpreter runs it in slices between stream chunks (or at once
  without a stream), never waiting inside a slice, so a retune does not stall
  a 320 Msps stream. Each program is stamped with the chunk numbers at which
  it started and finished, so samples after a change can be selected exactly.

What is controllable mid-stream: the LO of each A-die (any Hz it locks at,
found per unit at load; on its 2.98 Hz or 11.9–14.3 Hz grid), AGC on/off,
manual LNA and LPF gain, the analog filter bandwidth (20–320 MHz) and its RC
trim, and the captured band. Every band is set up at load, so switching bands
is one write of the capture engine's source field (ICAPCR8); the stream's rate
follows the band. A new sample rate is a RATE between streams.

The same mailbox also takes raw register programs (MMIO and A-die SPI) for
development and register discovery; regular operation uses only commands.

## Firmware

The MT7996's three processor types (WM, WA, DSP) are RISC-V. The ROM
authenticates neither the patch's hook table nor, behind our CRC hook, the RAM
images, so our code runs from the patch onward. See
[`firmware/README.md`](../firmware/README.md).

## Hardware reference

[`include/hw/`](../include/hw/README.md) documents every register the project
knows, as C headers; our firmware reaches registers only by these names.
