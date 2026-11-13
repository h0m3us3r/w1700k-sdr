# W1700K SDR

Turns a Gemtek W1700K Wi-Fi 7 router into a wideband software-defined receiver.
The router's MediaTek MT7996 radio captures raw 14-bit IQ at up to 320 Msps,
its Airoha AN7581 SoC streams it over 10 GbE, and a Linux host shows it as a
live spectrum and waterfall in the browser, with the receiver's controls next
to it.

![The 2.4 GHz band live: spectrum and waterfall](docs/images/live-2g4.gif)

*The whole 2.4 GHz band at 320 Msps, live, with the decimation's roll-off
corrected in the stream ("flatten"): a 262,144-point FFT of every sample, 100
spectra a second (this recording runs at 50 frames a second).*

The MT7996 runs only our own firmware: a ROM patch, WM and WA built from this
repository, with no MediaTek code. It brings the radio up from a cold power-on
by itself, from MediaTek's initialisation and calibration decoded register by
register against a trace of MediaTek's own firmware, and takes its commands
(LO, sample rate, capture, stream, gain, filter) from the host. See
[docs/architecture.md](docs/architecture.md) and [docs/roadmap.md](docs/roadmap.md).

## What it does

| | |
| --- | --- |
| Sample rates | 40, 80, 160 or 320 Msps of 14-bit complex IQ on every band, independent of the LO and the filter; on band 2 also 640/2 (every other sample of 640 Msps: flat to ±155 MHz, but signals 160–320 MHz away fold in) |
| Bands | 2.4 GHz (MT7976), 5 GHz and 6 GHz (two MT7977s); switching bands is a register write inside a running stream |
| Tuning | No channels: the LO is any Hz a band's synthesizer locks at (unit A: 2.081–2.748, 4.647–7.509 and 4.707–7.576 GHz), on its grid of 2.98 Hz (2.4 GHz) or 11.9–14.3 Hz steps; retuned mid-stream (about 1 ms) |
| Front end | AGC or manual LNA/baseband gain; analog filter 20–320 MHz |
| Transport | the radio DMAs into a 1 GiB ring in SoC DRAM; the kernel sends every 8 KiB slice as one UDP datagram, zero-copy, on the 10 GbE port; at 320 Msps half the slices are packed to 28 bits per sample pair on the radio's own cores to fit the link (lossless, automatic) |
| Slower links | the stream's packing follows the link's speed: every slice packed to 28 bits (lossless) carries 160 Msps on 5 Gbit/s, and 320 Msps fits it at 14 bits per sample pair, the radio and the SoC each requantising part; below that the SoC requantises to 16, 14, 8 or 7 bits per sample pair with a scale per slice (up to 160 Msps), and can send in datagrams small enough for a 1500-byte MTU; see the table below |
| Host | `w1700k-stream` receives the export and serves the IQ stream to every consumer on the host (shared memory, complex int16, gap-tagged), with an optional correction ("flatten") of the decimation's roll-off applied to the samples themselves; `w1700k-spectrum`: 262,144-point FFT at 100 frames/s over every sample, browser UI, live controls; `sdr-recv` records the stream; a SoapySDR module (`driver=w1700k`) gives it to GNU Radio and other SoapySDR programs |

## Links slower than 10 GbE

The stream fits whatever the unit's port negotiates (a 5 or 2.5 Gbit/s adapter, a
1 Gbit/s port); `w1700k` reads the speed at each start and picks the packing, or
takes one set by hand (`--packing`, `export.packing`). A rate nothing fits is
refused with the rate the link would need.

| Link | 14-bit, lossless | Reduced on the unit |
| --- | --- | --- |
| 10 Gbit/s | 320 Msps (half the slices packed), 160 Msps raw | |
| 5 Gbit/s | 160 Msps (every slice packed, 4.5 Gbit/s), 80 Msps raw | 320 Msps at 14 bits per sample pair (4.6 Gbit/s) |
| 2.5 Gbit/s | 80 Msps (packed), 40 Msps raw | 160 Msps at 14 bits per sample pair |
| 1 Gbit/s | | 80 Msps at 8 bits, 40 Msps at 16 bits |

- **Lossless** packings are made by the radio's own cores and leave the SoC
  zero-copy: `raw` (32 bits per sample pair on the link), `half` (30) and
  `full` (28, up to 160 Msps).
- **Reduced** packings (`16`, `14`, `8`, `7`: 8, 7 or 4 bits, or 11 levels, for
  each of I and Q) are requantised on the SoC, every 2048 samples with a scale
  of their own, and scaled back to 14-bit samples by `w1700k-stream`, so
  consumers see no difference but quantisation noise about 53, 47, 28 and
  25 dB below each block's peak. On a 2.4 GHz test tone the noise floor rose
  0.6, 0.7, 3.1 and 5.8 dB against the lossless stream.
- **320 Msps on 5 Gbit/s** is the `split` packing, 14 bits per sample pair.
  No one part of the unit can requantise a 320 Msps stream: the SoC's memory
  bus carries about 1.0 GB/s of reads beside the capture (the stream is 1.28),
  and the radio's two cores are busy at half of it. So the radio reduces 10 of
  every 16 chunks itself, under a shift the SoC sets from the signal's level,
  and the SoC reduces the 6 between. Measured on a 5 Gbit/s link in runs of
  five and ten minutes: complete, none of 48 and 94 million chunks lost, with
  a test tone at the same level and phase in the chunks from both. (That is
  the unit and the stream service. With the spectrum server and a browser
  also running on that host, an 8-core N350 behind a USB adapter, the
  adapter itself missed 1 datagram in 2400, and between 1 in 25,000 and 1 in
  100,000 once its packets were spread over the CPUs, as `init/host/link` now
  does: the host, not the link.) The radio's chunks follow the level with a second's memory, so
  in a band with strong bursts they are quantised for the bursts.
- **No jumbo frames?** `"export": {"piece": 1456}` in the configuration sends
  every slice as datagrams a 1500-byte MTU carries (each costs 82 bytes of the
  link; the unit is asked for at most 200 thousand a second: 80 Msps lossless,
  160 Msps at 8 bits).

The 5 Gbit/s row and the small datagrams are measured on a unit (no slice
lost); the 2.5 and 1 Gbit/s rows follow from the same arithmetic and have not
been run on such a link. The 10 Gbit/s row was run again on a W1700K2 after its
PHY driver was repaired ([platform/README.md](platform/README.md#the-10-gbe-ports-phys)):
320 Msps left the unit at a mean of 9.66 Gbit/s for two minutes without a
transmit error, into a second W1700K as the far end, and 160 Msps raw crossed
a cable between the unit's own two ports with no receive error counted. Details: [`host/w1700k/README.md`](host/w1700k/README.md).

## The front LED

The unit's status LED tells you what the receiver is doing without a screen
(details in [docs/led.md](docs/led.md)).

| | What you see | What it means |
| --- | --- | --- |
| <img src="docs/images/led/start.svg" width="48" height="20" alt="every colour once, out of the dark"> | Rainbow, once round, out of the dark | Starting: booted, the LED is ours from here |
| <img src="docs/images/led/no-link.svg" width="48" height="20" alt="yellow, breathing"> | Yellow, breathing | No link on the stream port |
| <img src="docs/images/led/idle.svg" width="48" height="20" alt="white, brightening"> | White, brightening from low to full | Powered and linked; radio not loaded (or loading) |
| <img src="docs/images/led/ready.svg" width="48" height="20" alt="green"> | Green, steady | Ready: radio loaded and tuned, not streaming |
| <img src="docs/images/led/rates.svg" width="48" height="20" alt="cyan, blue, magenta"> | Cyan / blue / magenta | Streaming at 40 / 80 / 160 Msps |
| <img src="docs/images/led/rainbow.svg" width="48" height="20" alt="every colour in turn"> | The rainbow, round every six seconds | Streaming at 320 Msps |
| <img src="docs/images/led/steady.svg" width="48" height="20" alt="steady"> | … at a steady brightness | lossless: every sample bit for bit |
| <img src="docs/images/led/breathing.svg" width="48" height="20" alt="breathing"> | … breathing | samples reduced to fewer bits to fit the link |
| <img src="docs/images/led/stalled.svg" width="48" height="20" alt="blinking slowly"> | … slow blink | started, but nothing is leaving the port |
| <img src="docs/images/led/warning.svg" width="48" height="20" alt="yellow, blinking"> | Yellow, blinking | Look: the captured band's synthesizer is out of lock |
| <img src="docs/images/led/fault.svg" width="48" height="20" alt="red, blinking"> | Red, blinking | Stop: the stream ended by itself |
| <img src="docs/images/led/heard.svg" width="48" height="20" alt="a short lift towards white"> | A short lift towards white | The host just changed something |

White then steady red is the loader and the kernel before our software runs;
red that never changes means the unit did not finish booting.

## Screenshots

![The web UI at 2.4 GHz](docs/images/ui-2g4.png)

*2.4 GHz at 320 Msps, flattened: Wi-Fi frames and shorter hopping bursts on a
floor that stays level to the band's edges. The Stream panel shows the link
carrying 9.75 Gbit/s, half of the slices packed, and every sample transformed.*

![The web UI at 5 GHz](docs/images/ui-5g.png)

*Two busy 80 MHz Wi-Fi channels at 5170–5330 MHz in a flattened 320 MHz view.*

![The two channels' edges zoomed to 24 MHz](docs/images/ui-zoom.png)

*Zoomed to 24 MHz around 5250 MHz: the two channels' edges and the guard gap
between them, in 1.22 kHz bins.*

## Repository

| Path | |
| --- | --- |
| [`platform/`](platform/README.md) | What runs on the SoC: our U-Boot, Linux 6.18 with board patches, a read-only BusyBox root |
| [`kernel/w1700k_sdr/`](kernel/w1700k_sdr/README.md) | The MT7996 driver (firmware download, the DMA ring, the 10 GbE export, the firmware's command mailbox, the status LED) and its unit-side clients |
| [`firmware/`](firmware/README.md) | Our MT7996 firmware: the ROM patch, the WM (radio bring-up and band set-up, streams, RF control) and the WA (packer) |
| [`host/w1700k/`](host/w1700k/README.md) | Host control: firmware load, streaming, RF control as firmware commands; `w1700k serve` for the web UI |
| [`host/stream/`](host/stream/README.md) | `w1700k-stream`, the host's one receiver of the export: the IQ stream in shared memory for every consumer, and the roll-off correction |
| [`host/spectrum/`](host/spectrum/README.md) | The spectrum/waterfall web server |
| [`host/recv/`](host/recv/README.md) | `sdr-recv`, the stream recorder |
| [`host/soapy/`](host/soapy/README.md) | The SoapySDR module (`driver=w1700k`): the receiver in GNU Radio and other SoapySDR programs |
| [`init/`](init/README.md) | Installation from stock, updates, recovery, return to stock |
| [`include/hw/`](include/hw/README.md) | Register reference: every known MT7996, A-die and AN7581 register, as C headers |
| [`docs/`](docs/) | [Architecture](docs/architecture.md), [the front LED](docs/led.md), [roadmap](docs/roadmap.md) |
| `tests/`, `tools/` | The test suite and its report page (`tools/test_report.py`), the on-unit sweep of the stream modes (`tools/unit_sweep.py`), a trial of the host's receive settings (`tools/host_rx_trial`), and the driver and notes for a USB adapter on the stream link ([`tools/r8152-rtl8159`](tools/r8152-rtl8159/README.md)) |

## Getting started

You need a W1700K (HW 1.x) or W1700K2 (HW 2.1), a 3.3 V USB serial adapter on
its console header, an Ethernet cable to its `lan3` port for installation, and
a 10 GbE NIC on the host cabled to its `wan` port (10GBASE-T; an X710 with a
copper SFP+ module works) for the IQ stream; a slower NIC or USB adapter works
at lower sample rates (see "Links slower than 10 GbE"). The stream port is
linked 5 to 6 s after it starts where the far end trains at what it offers.

A host without a 10 GbE port can use a USB adapter with Realtek's RTL8159,
about 80 dollars: it links at 5 Gbit/s (18 s after the unit's port starts),
which carries 160 Msps lossless and 320 Msps at 14 bits per sample pair, and
every mode of the 5 Gbit/s row above passes on a unit with one.
[`tools/r8152-rtl8159`](tools/r8152-rtl8159/README.md) has its driver for
kernels that lack it, what was measured, and how the host is set up for it. Both revisions run the same images.
Most units carry a W25N04KV NAND; some W1700K2s carry a W25N04LV (ID `ef 8b 23`),
which is supported but has never been run on a unit: `init/install` stops on
one until you pass `--i-dont-care-about-nand-corruption`, and asks for a raw
dump of it (`init/rawdump`) for the maintainers.

```sh
make                                   # everything, from public sources (~10 min plus downloads)
make check                             # the test suite; its report: tests/build/report.html

sudo init/host/link up IFACE           # 192.168.1.10/24 on the NIC cabled to lan3
sudo init/host/tftp-forward enable IFACE
init/install                           # back up the NAND, install, save the radio calibration

sudo init/host/link keep IFACE10G 10.77.0.10/24 9216  # the 10 GbE stream link: address, jumbo frames
host/stream/build/w1700k-stream &                      # the stream, for every consumer on this host
host/spectrum/build/w1700k-spectrum --controller "host/bin/w1700k serve" --listen 0.0.0.0:8073
```

Then open `http://HOST:8073/` and press ▶. The first start loads the radio
firmware and brings the radio up (about 2 s). Without the browser, the same
operations are `host/bin/w1700k load|start|band|tune|gain|filter|stop|status`.

To update an installed unit after a `git pull`, over the network alone:

```sh
make
init/flash --rootfs --ssh              # the driver, the unit's programs and its start-up
```

The unit's loader writes the new root at its next boot, and puts the previous
one back if the new one is never confirmed; see
[init/README.md](init/README.md#updates) for that, and for the kernel and the
loader, which take the serial console.

Build requirements: a C/C++ compiler, make, git, Python 3.10+, the usual
Buildroot prerequisites, clang, ld.lld, llvm-objcopy and llvm-readobj (RISC-V
firmware), openssl (firmware container encryption), and
`libfftw3-dev` and `zlib` for the stream service and the spectrum server. No vendor firmware is
downloaded or needed.

## Testing

```sh
make check        # every test; needs no unit
make sweep        # every stream mode on a real unit, about five minutes
```

`make check` runs the suite as `python3 -m unittest discover -s tests` does and
writes a page of what happened, `tests/build/report.html`: every test by module
under what the module says it tests, failures with their tracebacks, skips with
their reasons. The tests stand in for the hardware: simulated units for
installation and updates, the radio's packing kernels run from their machine
code in an emulator, the loader's update script run against a model of the
flash, and the host programs fed over loopback.

`make sweep` (`tools/unit_sweep.py`) does what they cannot. With a unit
reachable and `w1700k-stream` running, it runs every band at every sample rate,
every other packing the link carries, and the 640/2 capture, a few seconds
each, and checks that the samples arrive at the rate set, that next to none are
lost, that they are the kind the packing makes, that their level is plausible,
that the synthesizer is in lock, and that the front LED shows the mode. Its
results join the report page.

## Read before use

- **Our firmware is receive-only.** It runs none of MediaTek's transmit
  calibrations and keeps the MAC's transmit path disabled; a gapless watch at
  5250 MHz saw nothing during a load, where MediaTek's firmware transmits a
  full-power burst. Still, do not cable an antenna port straight into a
  sensitive instrument without an attenuator.
- **Our ROM patch writes no eFuse.** MediaTek's ROM patches since 2025
  permanently program one eFuse gate bit on their first run (also with
  MediaTek's own driver); a unit that ever ran one has that bit set.
- **Installation replaces the stock operating system.** `init/install` first
  copies the NAND's data to the host; keep that copy. `init/restore` writes it
  back and returns the unit to its stock firmware.
- Accessing undocumented MT7996 or AN7581 addresses can hang the SoC or wedge
  the radio until a power cycle. `include/hw/` marks every known hazard.

## Licence

MIT, except the Linux driver and kernel patches (GPL-2.0-only), U-Boot patches
(GPL-2.0+), the driver's user-space header (GPL-2.0 WITH Linux-syscall-note)
and files derived from MediaTek's mt76 driver (BSD-3-Clause-Clear). See
[LICENSE](LICENSE).
