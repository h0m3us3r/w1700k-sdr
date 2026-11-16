# Roadmap

## Where it stands

The receiver works end to end with only our firmware on the MT7996: up to
320 Msps of 14-bit IQ from any of the three bands, packed on the radio to fit
one 10 GbE link, received completely by the host, shown live in the browser.
Mid-stream controls tune the LO, set AGC or manual gain, set the analog filter
and switch bands. A load brings a power-cycled radio up in about 2 s; streams
start and stop without a reload. No MediaTek code is loaded, and nothing
transmits.

Around that: a stream fits the link the unit's port negotiates (160 Msps
lossless and 320 Msps at 14 bits per I/Q pair on 5 Gbit/s, fewer bits below
that); the front LED says what the unit is doing; an installed unit is
updated over ssh, root and loader, with every update undone unless confirmed;
its login key is set, switched off and erased with the reset button, so a unit
can be handed over with no key on it; and a SoapySDR module gives the stream
to other programs. The serial console is still needed for a first
installation, the first loader and the kernel image.

## How the firmware became ours

1. **Our code on the MT7996.** The ROM does not authenticate the patch's hook
   table; a hook that makes the RAM-image CRC check pass lets our WM and WA
   load. A third hook counts the DSP as started, so no DSP image is needed.
2. **Discovery.** A minimal WM of ours could replay register state captured
   from MediaTek's initialisation, and a radio warmed by MediaTek's firmware
   received; a power-cycled one received once the ROM patch's radio init and
   the A-die writes of MediaTek's load were replayed too. That showed what
   matters, and was not the product.
3. **Trace and decode.** MediaTek's firmware was traced on a power-cycled unit,
   every D-die register access (the WM core's own debug triggers) and every
   A-die SPI access (the ROM's SPI slots) in one ordered log with the code
   address of each, one calibration item at a time over 69 cold loads. Every
   procedure of the load was then regenerated record for record from
   MediaTek's code and tables, taking only read values and the EEPROM as
   inputs: the ROM patch's radio init, the common and per-band PHY init, the
   A-die RF init, every calibration item, the channel sets, and the capture
   set-up of the SPECTRUM command. Their registers and fields are in
   `include/hw/`.
4. **Reduce.** Measured on the bench against MediaTek's firmware: reception
   needs the RF init, the R-cal and RC calibrations and the synthesizer; the
   DC-offset calibration lowers the DC spur; the TTG IQ calibration's filters
   sit after the capture tap and do not touch the stream; every TX calibration,
   PHY table A, the 320 MHz background receiver and the channel set's MAC words
   can go. The MAC's receive path must be enabled (its transmit path stays
   off).
5. **Write it.** Each procedure was written in C for our WM from the headers,
   and checked access for access against the trace on the host before it ran
   on hardware. On a power-cycled unit the result leaves the radio as
   MediaTek's load does, register for register apart from measured
   calibration codes, and receives as well on every band.
6. **Commands.** Streams, band set-up, rate and RF control (tune, gain,
   filter, trim, band switch) are commands to our firmware, one per parameter,
   with no channel numbers; the host writes no radio
   register, and the vendor path (MediaTek's firmware, the replay, the
   SPECTRUM prime, the REG_ACCESS edit, the vendor download) is gone.

## Next

### A unit in someone else's hands

- **Updates for a unit installed elsewhere.** `init/flash --ssh` builds a
  unit's root on the host from the unit's directory there, which holds its ssh
  host key. A host that did not install the unit has no such directory, so it
  cannot update it. Either an "adopt" step makes the directory from the unit
  over ssh, or the host key moves out of the root, beside the paperclip key's
  slot, and one root fits every unit.
- **A signed update archive.** Every image is hashed before and after it is
  written, which guards against corruption and says nothing of who made it.
  A signature over the archive's manifest, checked by `init/flash --bundle`.
- **The kernel image over ssh.** It is a volume beside the root and could be
  staged and confirmed the same way; today it takes the console.
- **The first loader over ssh.** It is the one part a unit in the field can
  never have replaced. Two things would change that, and both have to be in
  place before a unit leaves, since stock's boot command and the first loader
  are written from the console:
  - Stock U-Boot carries on after a `bootm` that fails (measured: an image with
    a wrong hash, and erased flash, both return to the script, and `bootm
    damaged; bootm good` boots the good one). A boot command that tries
    `loader` and then `loader-b` would therefore survive a first loader torn by
    a power cut, and a confirmed second loader could then rewrite the first.
  - A watchdog armed by the first loader before it starts the second, so that
    one that hangs is reset without a hand on the plug.
- **The paths not yet run on a unit:** a console install with
  `--no-login-key`, and an RSA key handed over by the loader (both pass the
  tests and the loader's model).

### The GPS receiver

Decided and deferred: the receiver is off by default, switched off by the
loader at every boot, and `w1700k-gps on` runs it until the next start
(`platform/README.md`). Nothing reads it. To take up later:

- **An owner's switch that stays.** A setting in the unit's durable storage,
  read by the loader like its others, for a receiver that is on from boot.
- **Reading it.** The port enabled in the device tree, and a small reader on
  the unit. The driver should take both speeds, the 460800 the receiver uses
  and the 921600 the stock settings name, so that neither number is a guess;
  today it reaches 460800 and no higher.
- **What it is for.** Telling the units of a fleet apart and saying where each
  is; handing position and time to the host with the samples; time-stamping
  captures. There is no pulse-per-second line to the SoC that we know of.
- **What it is never for.** It is not a trust input: nothing a unit accepts
  is judged by GPS time, which anyone nearby can transmit. And where a unit
  is leaves the unit only by its owner's choice.

### The control plane

- **Unit-side control daemon.** The control plane is small (load, configure,
  start, stop, RF commands). A daemon on the unit can serve it on a socket, so
  the host no longer needs ssh for it, and a login key is for administration
  alone.
- **A dedicated command ring.** Commands use words in the firmware's mailbox
  page, relayed by the stream pump while it runs. A WFDMA command/event ring
  pair of its own (or the MT7996's second PCIe host interface) would give
  events and queueing. It would also let code be called on the WM during a
  stream, which the mailbox cannot do: the WM is inside the producer then.
- **Sample rate mid-stream.** A new sample rate is a RATE between streams; it
  could run inside a stream as a resumable program.

### Using the samples

- **Lower sample rates.** The stream and the SoapySDR module offer the
  radio's own rates, 40 Msps and up, which many SDR programs cannot take.
  Decimation in the stream service would serve them.
- **Calibrated levels.** dBFS to dBm per band and gain setting.
- **Frequency coverage.** Measured LO coverage is 2.081–2.748 GHz and
  4.647–7.576 GHz (unit A; above 7.41 GHz by the lock flag only). Below and between needs external conversion, or capture of the
  2.4 GHz monitor receiver (chain 4 of band 0), whose own PLL is known but whose
  capture path is not.
- **Fewer datagrams.** At 320 Msps on 5 Gbit/s the host takes 127,000
  datagrams a second, and a single-queue adapter is where slices are lost.
  Two of the radio's chunks to a datagram would halve that.

### Processing on the chip

- **The DSP cores.** Three RISC-V cores, one per band, sit idle under our
  firmware. A memory-to-memory DMA path into a core's data memory was found
  by a contributor and reproduces here (8 KiB in 8.8 us); posted from the
  host it ran beside a raw 80 Msps stream with no slice lost. With a packed
  stream its descriptors and source were in memory the WM's packers rewrite
  (`include/hw/mt7996/mcu_map.h`), so that case is open: the rings need a
  place of their own, and the transfers a driver inside the producer.
- **What the cores are for.** Not the stream itself: the path carries
  0.9 GB/s against the 1.28 of 320 Msps, and a core has four clock cycles a
  sample at 160 Msps. A share of the chunks, one way, is enough for a
  detector: an occupancy map per band, a squelch or trigger for a capture, a
  faster level meter for the shift of the split packing.

### Understanding the radio

- **The tables as formulas.** SETUP still applies MediaTek's per-frequency-group
  and per-bandwidth-class tables (and a TUNE does not reload the group tables);
  each should become a computed function of the LO and the sample rate.
- **More of the radio understood.** Many A-die registers of the RF init tables
  have no known function yet; they carry offset names and MediaTek's values.

### Proving it

- **Long runs.** A 24-hour stream with the export validated. The longest so
  far is 9 h 23 min at 320 Msps on a 5 Gbit/s link, the samples not checked:
  1 slice in 3.6 million lost, every one of them a datagram the host's adapter
  missed ([the measurements](../tools/r8152-rtl8159/README.md)).
- **Links not yet run.** The 2.5 and 1 Gbit/s rows of the link table are
  arithmetic; 10 Gbit/s was run into a second unit as the far end, not into a
  host.
- **The second loader's failures** have now happened on a unit and recovered
  as the loader's model said: one that hangs, one that never boots the system,
  and a `loader-b` damaged at rest in four ways, each repaired over ssh
  afterwards (`init/README.md`, "The loader"). Still only in the model: power
  lost while the first loader writes or removes `loader-b`. And a loader that
  hangs for good waits for someone to cut the power; nothing resets the unit.
- **One test that fails now and then under load** (the spectrum server's
  `tuning_steady`).

## Release checklist

- [x] Only our firmware on the MT7996, bringing the radio up from cold by
      itself, controlled only through commands.
- [x] Our firmware reaches registers only by their header names.
- [ ] Every known register named in `include/hw/` (offset names stand in where
      the function is unknown).
- [x] A clean clone builds (`make`) and passes `make check`; its firmware is
      byte-identical to the development tree's.
- [ ] The same on a fresh machine (only the development machine has built it).
- [x] Install and return to stock run on a second unit (a W1700K2).
- [x] Updates run on a second unit (a W1700K2 from the factory: installed,
      every stream mode, then `init/flash --ssh`).
- [ ] Recovery (`init/flash --recovery`) run on a second unit.
- [ ] A W25N04LV unit (some W1700K2s) read raw and installed: the part, its
      spare layout and its vendor bad-block reserve are supported from three
      community driver tables and the W1700K's stock U-Boot, none read on one.
