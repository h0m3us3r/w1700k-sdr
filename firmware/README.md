# MT7996 firmware

Our firmware for the MT7996: a ROM patch, a WM image and a WA image, built from this directory
with no MediaTek file. `make -C firmware` writes them to `firmware/build/`:

| file | role | what it is |
|---|---|---|
| `w1700k_rom_patch.bin` | patch | only our three ROM hooks, no MediaTek body (`rom_patch/`) |
| `w1700k_wm.bin` | wm | our WM: the radio bring-up and band set-up, the stream producer, RF control (`wm/`, `radio/`, `stream/`) |
| `w1700k_wa.bin` | wa | our WA: the stream packer (`wa/`, `stream/`) |
| `firmware.json` | | role, file, size, sha256 and download flags of each, in download order |

Requirements: Python 3.10+, `clang` with the RISC-V target, `ld.lld`, `llvm-objcopy`,
`llvm-readobj`, `openssl` (AES for the container regions).

The MT7996's three processor types (WM, WA, DSP) are RISC-V (Andes). The ROM authenticates
neither the patch's hook table nor, behind our CRC hook, the RAM images, so our code runs from
the patch onward. No DSP image is loaded.

## ROM patch (`rom_patch/`)

The patch holds a 64-entry hardware instruction-redirect table; our container
(`patch.py` `build_own`) is an empty table and a blank security footer to which three hooks are
added. Each replacement runs our code, then a copy of the hooked instruction, then jumps back
into ROM after it; the build refuses an object with relocations and re-decodes its output.

- `hook-pdacrc-result.S` at ROM `0x008264e4`: makes `halWfdmaPdaCheckCRC` report a match, so
  our RAM containers load. The PDA idle, length and valid-bit checks still refuse a download
  that did not land.
- `hook.S` at ROM `0x00822b94`: repartitions the UMAC SYSRAM pool to ICAP 512 / PLE 64 / PSE
  128 banks with ICAP at position 0, so the capture engine owns pool bytes 0..1 MiB (MCU
  `0xe0400000`..`0xe04fffff`). Our WM's bring-up makes the ROM call it hooks.
- `hook-start-mask.S` at ROM `0x00838d6c`: the ROM starts no core until the WM, the WA and the
  DSP have each had a FW_START; the hook counts the DSP as started, so no DSP image is needed.

None of MediaTek's 26 ROM redirects is in it, and none of the eFuse "recovery" their 2025 and
later patches run. It loads on parts with unprovisioned security fuses.

The set is written for one ROM. A hook is a ROM instruction's address and the bytes found
there, the hooks call ROM functions by address, and so does the WM. In another revision of the
ROM those addresses are the middle of something else, and the ROM holds a routine that
programs eFuses. So `rom_patch/patch.py` names the ROM by the SHA-256 of its first 64 KiB (the
part the driver lets a host read), `firmware.json` carries that, and the host reads the ROM
after the reset and downloads nothing onto any other (`host/w1700k/radio.py`). The hash is
that of one hardware 2.1 unit. A radio that hashes otherwise is refused with the hash it has,
to report; `"unknown_rom": true` in the host's configuration loads all the same, for studying
such a revision and at that radio's risk.

## The WM (`wm/`, `radio/`)

- `wm/start.S`: entry from FW_START at `0x00916400`; records the core as the ROM handed it
  over, installs a trap handler (a faulting load or store is recorded and skipped, anything else
  parks the core) and sets its stack.
- `wm/wm.c`: drops the D-cache and invalidates the I-cache (the ROM leaves lines valid over
  CRAM, where our download then writes the radio code), takes the ROM's gp, sets both CPUs to
  720 MHz, sends the FW_START reply the ROM leaves to the started firmware, masks every
  interrupt source and publishes its status block at `0x0090f300` (`wm/wm.h`). Its main loop
  serves radio commands and runs the control interpreter (`stream/ctl.h`).
- **Radio commands** (`radio/radio_cmd.h`), posted by the host in the status block and
  acknowledged there with a result, the first hardware failure and the time taken:
  `BRINGUP` (the radio from cold), `SETUP band lo bw` (the band at an LO in Hz and a bandwidth
  class: 20, 40, 80 or 160 MHz for 40 to 320 Msps, and on band 2 also 320 MHz, captured at
  640 Msps of which the stream keeps every other sample;
  everything MediaTek's channel set wrote, computed from those, the synthesizer by TUNE's own
  program, then the RX DC-offset calibration; no channel numbers), `RATE band bw` (the bandwidth
  alone: no synthesizer, no DCOC, LO and filter kept), `DCOC band` (the DC-offset calibration at
  the current LO and filter), `CAPTURE band bw` (the capture path and the MAC receive enable),
  `SELECT band`, `STREAM packing` (acknowledged when the stream ends), and `STEP` for
  development. The LO (TUNE: any Hz the synthesizer reaches, on its 2-LSB grid), the analog filter
  (FILTER), gain and trim are RF commands of their own; the filter is independent of the
  bandwidth and no SETUP or RATE touches it.
- **The radio code** (`radio/`, in a CRAM region at `0xe0040000`, above the ROM's download
  buffers): MediaTek's cold initialisation and channel set, decoded record for record against
  its traced cold load and written from the register headers (`include/hw/`), each module
  checked access for access against that trace on the host (`radio/tests/mock.c`):
  - `patch_init.c`: what MediaTek's ROM patch does at download (CBTOP and XO, pin mux, the
    CONN AFE calibration pulse of every band, the A-die TOP init by chip ID, pads and CONN
    switch, the A-die eFuse trims, read through the ROM's read-only reader);
  - `bandinit.c`: the common PHY init (FEM and antenna-switch pin mux), the per-band PHY init,
    and the tail (FEM outputs, crystal trim);
  - `rfinit.c`: calibration item 0 (the A-die RF init tables) with R-cal and the RC
    calibration;
  - `chanset.c`: SETUP, MediaTek's receive-only channel set driven by the LO and bandwidth
    alone (per-frequency-group A-die tables, PHY table B, the RX front end, the D-die RX setup,
    the RX gain offsets; the synthesizer by `rfctl.c`'s TUNE program, run by `rfrun.c`), and RATE
    (the same without the synthesizer); band 0 at 80 and 160 MHz is ours, MediaTek's 2.4 GHz
    tables ending at 40 MHz (`chanset.h`);
  - `dcoc.c`: the RX DC-offset calibration, at SETUP and on its own command;
  - `capture.c`: the capture path as MediaTek's SPECTRUM command leaves it;
  - `rfctl.c`: RF command programs (tune, gain, filter, trim, status, locks, band switch); band
    0's 320 MHz filter is the MT7976's widened code 3 (`include/hw/adie/mt7976.h`);
  - `stream_arm.c`: the capture route and ICAP0 around a stream;
  - `hal.h`, `hal_wm.c`: the only hardware access: D-die loads and stores, one A-die SPI
    transaction per access, delays, polls, a failure record.
  Reception uses only what the receive path needs: no transmit calibration, no TX power, no
  TTG IQ calibration (its filters are after the capture tap), MAC transmit kept disabled.

## Streams (`stream/`)

- `producer.c`, linked into the WM at `0x00930000` and entered by the STREAM command with the
  packing schedule's code and interrupts off: waits for the pump's go word, runs
  ICAP0 as a ring over the 1 MiB partition, follows its write pointer (CR9) and publishes each
  8 KiB chunk on MCU_DMA0 TX ring 0, which lands in the host's WFDMA RX ring 0. It runs until
  the stop word (same address as the go word) or an overrun, drains TX0 before stopping ICAP
  and writes its results at `0x0090f000`. Between chunks it runs slices of control programs.
- `ctl.h`: the control mailbox at `0x0090f400` and its resumable interpreter: RF commands
  (expanded by our WM's builder, `radio/rfctl.c`) and raw ops (MMIO and A-die SPI reads and
  writes, delays, polls) for development and register discovery. A slice never waits, so a
  retune does not stall a 320 Msps stream.
- `packer.c` and `pack.S`, linked into the WA at `0x1001a400`: 28-bit packing, 8 words to 7,
  so 320 Msps fits one 10 GbE link; the producer raises the WA through a soft-interrupt slot.
- `layout.h`: every fixed address and protocol word; `blob.py`: the compile/link recipe and the
  packing schedules, a table in both images that the STREAM command indexes, over a period of
  32 chunks (raw; half = WM 8 + WA 8, for 320 Msps; full = WM 16 + WA 16, every chunk, up to
  160 Msps, where each packer is 62 % busy; split = WM 9 + WA 11 reduced to 14 bits, see below).
- `reduce14.S`: the split schedule's kernel. A 320 Msps stream on a link too slow for half
  (5 Gbit/s) must have every chunk at 14 bits per sample pair, and neither the radio's cores nor
  the SoC can requantise all of it; so the packers reduce 10 of every 16 chunks here (7 bits of I
  and of Q under one shift per chunk, two samples folded into a word and those packed 8 to 7:
  3584 bytes and a trailer naming the shift, `include/hw/mt7996/iq_format.h`), and leave 6 raw
  for the SoC. 87 instructions per 16 samples against the pack's 75. The WM takes 9 of every 20
  such chunks and the WA 11: at 10 each the WM, which also publishes every chunk and runs the
  control programs, had no time left at 320 Msps, and a retune during a stream took seconds;
  the WA, which only packs, is 97 % busy with its 11. The shift comes from the host (the mailbox word `MT7996_PROBE_WM_SHIFT`,
  which the producer relays to the WA): the cores have no cycles left to measure a chunk's
  level, and the SoC sees the level in the chunks between. `tests/test_firmware_kernels.py`
  runs this code and `pack.S` from their machine code against the format's model.

## The WA (`wa/`)

Our WA sets up its own memory attributes and caches (it leaves reset with both off; the packer
reads the pool through the D-cache), then polls the soft-interrupt action word and calls the
packer when the producer raises it. Our WM starts the WA for the first half-packed stream, once
the radio is configured: a WA started earlier leaves the receiver without antenna signal. Its
status block is at `0x0090f3e0`.

## Containers (`own.py`, `container.py`, `aes.py`)

`container.py` seals code into the RAM container format (one AES-128-CBC region per memory, an
empty release envelope, the outer CRC32). The WM container holds three regions (ULM1 entry, the
producer, CRAM), the WA container two (entry, packer).

## Hazards

- The ROM's download writes its command packets into 2 KiB buffers in CRAM below `0xe0040000`;
  code loaded there is corrupted (`include/hw/mt7996/mcu_map.h`).
- A read of an address that does not answer faults our WM's load; the trap handler skips it,
  and such a read returns nothing usable.
- Register hazards are marked `HAZARD:` in `include/hw/`.

### The guard

Our WM runs raw programs for a host: any MCU address, any A-die register, and a call to any
address. A few of those are not a host's to reach by mistake, so our WM starts **locked** at
every load, and locked it does not run a raw op of a host's program that reaches
(`stream/guard.h`, the list as it is):

- the MAC's transmit and receive disables (written by the radio code alone), a chain's
  transmit blocks or transmit detectors, and the external front end's pins;
- the eFuse controller, read or written;
- the CONN semaphores, read or written (a read takes one), a capture route grant, a raw
  chip-select, and the A-die reads and writes that the headers mark as wedging a die.

Such an op ends its program with status 9, "denied". A call (`WM_CALL`) goes into our own
image and nowhere else; a refused one is counted in the status block. The firmware's own
programs, which an RF command is made of, are not judged.

`"raw_access": true` in the host's configuration writes the unlock word after every load, and
the guard is lifted until the next one: for development, such as code of your own loaded and
called on the WM. The guard keeps a mistake out. It is no protection against the host, which
loads the firmware and can unlock it, and it lists what is known to be dangerous, not
everything that might be.
