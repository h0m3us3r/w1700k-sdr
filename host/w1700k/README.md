# w1700k: host control

Python 3.10+ package that drives a W1700K over ssh: loading our MT7996 firmware, IQ streaming to
the host over 10 GbE, and live RF control. Every operation is a command to our firmware; the host
writes no radio register. Standard library only; the unit side is the `w1700k_sdr` driver and
its clients.

Run it from a checkout, with either entry point:

```sh
host/bin/w1700k status
PYTHONPATH=host python3 -m w1700k status
```

Build the firmware first (`make -C firmware`); the host reads `firmware/build/firmware.json`.

## Configuration

`~/.config/w1700k/config.json` (or `$W1700K_CONFIG`, or `--config`); every key is optional:

| key | default | meaning |
|---|---|---|
| `unit.host`, `unit.user`, `unit.ssh_key` | `192.168.1.1`, `root`, ssh default | ssh target |
| `unit.known_hosts` | ssh default | the unit's host key, e.g. the file `init/provision` wrote for it |
| `unit.bin_dir` | `/usr/sbin` | unit-side clients |
| `unit.module` | `/lib/modules/w1700k_sdr.ko` | driver loaded by `load` |
| `eeprom` | see below | the unit's calibration EEPROM |
| `firmware_dir` | `<repo>/firmware/build` | built firmware set |
| `export.interface`, `src_ip`, `dst_ip` | `eth1`, `10.77.0.1`, `10.77.0.10` | 10 GbE path |
| `export.dst_mac` | MAC of the local interface holding `dst_ip` | |
| `export.port`, `export.ports` | `7700`, `8` | UDP destination port; source ports cycle over `ports` |
| `export.link_mbps` | the unit port's negotiated speed | what a stream may put on the link, Mbit/s, where that is less (a USB adapter that carries less than its Ethernet side links at) |
| `export.packing` | automatic | `raw`, `half`, `full`, `split`, or `16`, `14`, `8`, `7` (bits per I/Q pair): every stream's packing set by hand, where the automatic choice is wrong for a link |
| `export.piece` | whole slices | bytes of a slice per datagram, 256..8192, for a path without jumbo frames (`1456` fits a 1500-byte MTU) |

All ssh traffic shares one ControlMaster connection (socket in `$XDG_RUNTIME_DIR`).

**Calibration EEPROM.** Each unit carries its own RF calibration, a 16 KiB EEPROM image
(7680 bytes used) in the factory `calibration` partition at NAND 0x405000. `init/install`
saves it from the unit's backup to `~/.local/share/w1700k/units/SERIAL/eeprom.bin`
(`init/eeprom --unit SERIAL` does it for an existing backup). Without an `eeprom` setting the
host uses `~/.config/w1700k/eeprom.bin` if present, else that saved copy when exactly one unit
has one. It is written into our WM's EEPROM buffer on every load, before the bring-up; nothing
is written to the unit's flash or eFuses.

## Commands

| command | |
|---|---|
| `load [--band B]` | load our firmware, bring the radio up from cold, every band at its LO and sample rate; B captured (about 2 s) |
| `band B [--rate RATE] [--lo FREQ]` | capture band B; another rate (40, 80, 160, 320 Msps, or `640/2` on band 2) or LO |
| `start [--packing P]` | start streaming (packing follows the rate and the link, see below; `--packing raw\|half\|full` sets this stream's by hand) |
| `stop` | stop streaming |
| `status [--rf]` | the state below; `--rf` reads the active band's synthesizer, filter and gain |
| `tune FREQ` | LO of the active band, any Hz it locks at (`5250M`, `2.442G`, `2441.000123M`) |
| `gain --agc` / `gain --lna 0..4 --lpf 0..9` | AGC or forced gain |
| `filter 20\|40\|80\|160\|320` | analog filter of the active band, at any sample rate |
| `trim 0..31` | filter RC trim |
| `serve` | the JSON control protocol below |
| `push [--from DIR] [--to DIR]` | development: copy a fresh driver and the clients to `/tmp/w1700k` |
| `fw` | development: our WM's and WA's status blocks (`wm.py`) |

After `push`, set `unit.bin_dir` to `/tmp/w1700k` and `unit.module` to
`/tmp/w1700k/w1700k_sdr.ko`; the next `load` replaces the running driver.

There are no channels. A band is three independent settings: its LO, its sample rate and its
analog filter. Defaults, all set by every load:

| band | radio | LO | sample rate | filter |
|---|---|---|---|---|
| 0 | 2.4 GHz, MT7976 | 2442 MHz | 320 Msps | 320 MHz |
| 1 | 5 GHz, MT7977 | 5250 MHz | 320 Msps | 320 MHz |
| 2 | 6 GHz, MT7977 | 6105 MHz | 320 Msps | 320 MHz |

- **LO**: any Hz the band's synthesizer locks at; a load finds each band's span on the unit
  (`rf.lo_range_hz`; unit A: band 0 2080.7-2748.0 MHz, band 1 4646.6-7509.0, band 2
  4707.2-7576.2, 1 MHz kept inside the edges). The LO lands on the synthesizer's grid: steps of
  2.98 Hz on band 0, 11.92 Hz (below 5926 MHz) or 14.31 Hz on bands 1 and 2
  (`include/hw/adie/sx.h`); the state reports the LO realised.
- **Sample rate** 40, 80, 160 or 320 Msps on every band: the radio's 20/40/80/160 MHz bandwidth
  class (the code SETUP and RATE take), sampled at twice that. At 320 Msps the digital channel
  filter rolls off beyond about +-110 MHz, signal and noise alike (-9 dB at +130 MHz on band 2,
  where the SNR stays that of mid-band). **640/2** (band 2 only): the 320 MHz class, captured at
  640 Msps, every other sample streamed: 320 Msps flat to about +-155 MHz, but signals 160..320 MHz
  from the LO fold in at -2..-32 dB; the state reports it as `capture_rate` 640000000. The other
  bands have no 640 Msps datapath.
- **Packing** follows the rate and the link (the unit port's negotiated speed, read at each start,
  or `export.link_mbps` if lower); lossless where the link allows. `raw`: 14-bit IQ as
  captured, where it fits (160 Msps on 10 GbE, 80 on 5 Gbit/s, 40 on 2.5). `full`: every chunk
  packed to 28 bits (8 samples in 7 words) by the WM and the WA, 7/8 of the bytes, up to 160 Msps
  (160 on 5 Gbit/s, 80 on 2.5). `half`: every other chunk packed, which is all the two packers
  carry at 320 Msps (10 GbE). Where none of those fits, the unit's SoC requantises each chunk to
  fewer bits with a scale of its own and sends it itself (`mt7996-stream --send`): `16`, `14`, `8`
  or `7` bits per I/Q pair (8, 7 or 4 bits, or 11 levels, per component), the most that fit. That
  is not lossless: the quantisation noise lies about 53, 47, 28 and 25 dB below each chunk's
  peak. It carries 160 Msps on 2.5 Gbit/s (`14`), 80 Msps on 1 Gbit/s (`8`) and 40 Msps on
  1 Gbit/s (`16`), and stops at 160 Msps: reducing means reading every sample through the SoC's
  memory bus, which carries 1.0 GB/s beside the capture, and 320 Msps is 1.28 (measured: one core
  reads 0.60 GB/s, two 1.0, three 0.93; the zero-copy export of the lossless packings reads
  nothing). At 320 Msps the `split` packing shares the work instead: the radio's cores reduce
  10 of every 16 chunks to 14 bits (7 bits of I and of Q under a shift per chunk, which
  `mt7996-stream` sets from the levels its senders see, wide enough for twice the largest of the
  last second) and the driver exports those zero-copy, while the senders reduce the 6 raw chunks
  between: 4.6 Gbit/s, which a 5 Gbit/s link carries. It needs the module and the clients of
  this build (the export's `W1700K_SDR_EXPORT_SHORT`). A stream may take 97.5 % of the link; a
  rate that no packing fits is refused with the rate the link would need. Set by hand
  (`export.packing`, `start --packing`, `serve --packing`), the packing is used whatever the
  link; only `full` and the numbered ones above 160 Msps, and `split` at or below it, are
  refused; the state reports it as `stream.packing_manual`.
- **Small datagrams** (`export.piece`): each chunk leaves as datagrams of at most that many of
  its bytes, through the unit's senders, whatever the packing: for a host interface or a path
  that carries no jumbo frames. Each datagram costs 82 bytes of the link and CPU on both ends;
  the senders are asked for at most 200 thousand a second (80 Msps packed or 160 Msps at `8` in
  1456-byte pieces), and the packing choice counts that in. Every rate from 80 to 239 thousand
  was clean with the packets counted at the unit's port, 200 and 239 thousand for two minutes
  each; but five runs in a row at 235 thousand once lost 0.1–0.2 % inside the unit for a reason
  not found, so the limit stays under that.
- **Filter** 20..320 MHz. It follows the sample rate by default (as wide as the rate, 320 MHz at
  640/2): it is the receiver's only anti-alias filter, and whatever it passes 320 MHz from the LO
  folds onto the band (18..20 dB down at 320 MHz, 32 at 160, 45 at 80, 65 at 40, 77 at 20, measured
  at 160 Msps on band 0). A filter set by hand (`filter MHZ`) stops them following, on every band,
  and is kept across LO and rate changes until `filter auto`; the state reports
  `rf.filter_follows_rate`.
  On band 0 the MT7976 has no 320 MHz filter code: 320 is its 160 MHz code widened (about -3 dB at
  +-100 MHz at 320 Msps, `include/hw/adie/mt7976.h`).

## Behaviour

- A load downloads our firmware set (ROM patch, WM, WA), writes the EEPROM into our WM, and
  sends BRINGUP (the radio from cold: the patch's radio init, PHY init, RF init and calibrations
  on every die), then per band FILTER (its analog filter), SETUP (everything that follows from the
  LO and sample rate, the synthesizer, the DC-offset calibration at that filter) and CAPTURE
  (capture path, MAC receive enable), the captured band last; then each band's lock span is
  probed (TUNE bisection, then back to its LO) and SELECT points the capture at the band.
  About 2 s from a cold radio.
- The receiver state lives on the unit (`/tmp/w1700k-state.json`, cleared by a reboot), so CLI
  calls and a `serve` session share it. Streams run detached on the unit; each is one STREAM
  command to our WM, acknowledged when it ends. Streams start and stop without a reload.
- RF control (tune, gain, filter, trim, band switch) is one RF command each, which our WM
  expands and runs between stream chunks, or at once without a stream. A tune also rewrites the
  D-die words that follow the LO. Samples captured during a retune are invalid. The per-frequency
  calibration tables a SETUP applies (MediaTek's frequency groups) are not reloaded by a tune.
- `band`/`configure` to another band switches the capture source (ICAPCR8) with an RF command,
  also mid-stream; a new LO is a TUNE, also mid-stream. A new sample rate stops a running stream,
  runs RATE and CAPTURE (no synthesizer retune: the LO, gain and filter stay) and restarts it,
  packed as the new rate needs. A stream switched to a band whose rate its packing does not carry
  on the link restarts with the packing that does (raw to a 320 Msps band: half); otherwise it
  keeps running. A change the link cannot carry at all is refused and the stream stays as it was.
- After a load, a stream start and a band switch, every band's synthesizer lock is read
  (`radio.synth_locked`). A firmware reload does not reset the RF dies: a band whose
  synthesizer stays unlocked needs a power cycle of the unit, and is reported as an error.
- The export sends one 8 KiB slice per datagram: the unit's export interface needs a jumbo MTU
  (at least 8236; 9216 provisioned), checked before each start.
- Nothing transmits: our firmware runs no transmit calibration and keeps the MAC's transmit
  path disabled.

## `w1700k serve`

One JSON object per line on stdin; one reply per request `{"id":N,"ok":true,"state":STATE}` or
`{"id":N,"ok":false,"error":"..."}`; events without `id` in between. `status` is answered
immediately; other requests run one at a time in order.

| request | |
|---|---|
| `{"id":1,"op":"status"}` | |
| `{"id":2,"op":"load","band":1}` | `band` optional |
| `{"id":3,"op":"configure","band":2,"sample_rate":320000000,"capture_rate":640000000,"lo_hz":6105000000}` | `sample_rate`, `capture_rate` (default: the sample rate; 640000000 with 320000000 is 640/2 on band 2), `lo_hz` optional; restarts a running stream for a new rate |
| `{"id":4,"op":"start"}` | |
| `{"id":5,"op":"stop"}` | |
| `{"id":6,"op":"tune","hz":5300000000}` | the active band's LO, any Hz in its span |
| `{"id":7,"op":"gain","agc":true}` / `{"id":7,"op":"gain","lna":2,"lpf":9}` | |
| `{"id":8,"op":"filter","mhz":80}` | the active band's filter by hand; the filters stop following the sample rate |
| `{"id":9,"op":"filter_follow","on":true}` | every band's filter follows its sample rate (the default), or stays as it is |

Events: `{"event":"state","state":STATE}` on every change, `{"event":"progress","text":...,
"fraction":0.4}` during a load, `{"event":"log","level":"info|warn|error","text":...}`; a
stream that ends on its own (overrun, lost source) is reported as an error in STATE.

```json
{"busy": null,
 "radio": {"loaded": true, "band": 1, "rf_band": "5g", "lo_hz": 5250000000, "sample_rate": 320000000,
           "capture_rate": 320000000,
           "bands_ready": [0, 1, 2], "synth_locked": [true, true, true]},
 "stream": {"running": true, "packing": "half", "packing_manual": false, "link_mbps": 10000,
            "sample_rate": 320000000, "center_hz": 5250000000},
 "rf": {"lo_hz": 5250000000, "agc": true, "lna": null, "lpf": null, "filter_mhz": 320,
        "lo_range_hz": [4647562500, 7508048460]},
 "error": null}
```

`stream.sample_rate` and `stream.center_hz` label the spectrum axis; they change with tune,
band and configure.
