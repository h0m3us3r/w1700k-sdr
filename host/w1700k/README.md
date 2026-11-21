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
| `export.dst_mac` | MAC of the local interface holding `dst_ip` | unset, the stream comes to this host, and a start is refused where that interface's MTU is below the stream's packets (`sudo init/host/link keep IFACE 10.77.0.10/24 9216`) |
| `export.port`, `export.ports` | `7700`, `8` | UDP destination port; source ports cycle over `ports` |
| `export.link_mbps` | the unit port's negotiated speed | what a stream may put on the link, Mbit/s, where that is less (a USB adapter that carries less than its Ethernet side links at) |
| `export.packing` | automatic | `raw`, `half`, `full`, `split`, or `16`, `14`, `8`, `7` (bits per I/Q pair): every stream's packing set by hand, where the automatic choice is wrong for a link |
| `export.piece` | whole slices | bytes of a slice per datagram, 256..8192, for a path without jumbo frames (`1456` fits a 1500-byte MTU) |

All ssh traffic shares one ControlMaster connection (socket in `$XDG_RUNTIME_DIR`).

**Calibration EEPROM.** Each unit carries its own RF calibration, a 16 KiB EEPROM image
(7680 bytes used) in the factory `calibration` partition at NAND 0x405000. `init/install`
saves it from the unit's backup to `~/.local/share/w1700k/units/SERIAL/eeprom.bin`
(`init/eeprom --unit SERIAL` does it for an existing backup). The host loads the file the
`eeprom` setting names, else `~/.config/w1700k/eeprom.bin` if present. Without either it reads
the image from the unit it is talking to, out of that partition (which nothing of ours
writes), so a host with several units never gives one unit another's calibration. Only a unit
that gives none (a system without the partition) gets the saved copy, and only when exactly one
unit has one. The image is written into our WM's EEPROM buffer on every load, before the
bring-up; nothing is written to the unit's flash or eFuses.

Two units on one host have the same address and different ssh host keys. `init/install` writes
each unit's key to `~/.local/share/w1700k/units/SERIAL/known_hosts`; name the one in use as
`unit.known_hosts`, or keep both lines in `~/.ssh/known_hosts` (ssh accepts any of the keys it
holds for an address).

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
| `occupancy [--seconds S] [--per-second N] [--threshold DB] [--keep]` | what is in the air around the LO, measured on the radio beside the running stream (below) |
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
  +-100 MHz at 320 Msps on the die it was measured on, `include/hw/adie/mt7976.h`). It is widened
  by lowering the filter's trim, and at a low enough trim the widened filter oscillates: a line
  that wanders near +-145 MHz from the LO at about -20 dBFS, which the AGC takes for signal and
  answers with its lowest gain. The firmware keeps the trim out of those codes, so on a die that
  calibrates lower the 320 MHz filter is narrower rather than unstable.

## Behaviour

- A load downloads our firmware set (ROM patch, WM, WA), writes the EEPROM into our WM, and
  sends BRINGUP (the radio from cold: the patch's radio init, PHY init, RF init and calibrations
  on every die), then per band FILTER (its analog filter), SETUP (everything that follows from the
  LO and sample rate, the synthesizer, the DC-offset calibration at that filter) and CAPTURE
  (capture path, MAC receive enable), the captured band last; then each band's lock span is
  probed (TUNE bisection, then back to its LO) and SELECT points the capture at the band.
  About 2 s from a cold radio.
- The receiver state lives on the unit (`/tmp/w1700k-state.json`, cleared by a reboot), so CLI
  calls and `serve` sessions share it. Streams run detached on the unit; each is one STREAM
  command to our WM, acknowledged when it ends. Streams start and stop without a reload.
- Several controllers may drive one unit at once (the spectrum page's `serve`, a SoapySDR
  program's, a CLI call). Each reads the unit's state before every operation, and a `serve`
  session every two seconds while it has nothing to do: what another changed is adopted and
  reported as a state event, so a retune from one shows in the others within about two
  seconds, and an operation always acts on the receiver as it is. A long operation (load,
  configure, start, stop) is written to the unit before it begins: the other sessions report
  it as `busy`, wait for it before an operation of their own (up to two minutes, in case its
  session died), and do not take a stream it stops for one that ended by itself. A reboot of
  the unit is noticed the same way. Two operations sent at the same moment are not otherwise
  ordered: the later one to save its state wins.
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
  (at least 8236; 9216 provisioned), checked before each start. So does the interface that
  receives it: where the stream comes to the host the controller runs on (no `export.dst_mac`),
  a start is refused if that interface's MTU is below the stream's largest packet, since an
  interface drops what is larger without a word (a USB adapter plugged in again is back at
  1500). `sudo init/host/link keep IFACE 10.77.0.10/24 9216` sets it for good.
- Nothing transmits: our firmware runs no transmit calibration and keeps the MAC's transmit
  path disabled.

## Occupancy

`w1700k occupancy` says what is in the air around the active band's LO while a stream runs: for
each 64th of the sample rate, how strong it was and in what share of the time something was
there. The radio works it out itself: one of the MT7996's DSP cores is handed a chunk of the
stream now and then and keeps the account ([`firmware/README.md`](../../firmware/README.md),
"Occupancy"). The stream goes on untouched, and no consumer on the host is needed.

```sh
w1700k band 1 --rate 160 --lo 5180M && w1700k start
w1700k occupancy --seconds 2
```

On a unit, in a quiet hour (the `hz` values are the LO the synthesizer's grid gives):

```json
{"report": 13, "snapshots": 200, "snapshots_since_on": 200, "bin_hz": 2500000.0, "peak_dbfs": 0.0, "gain_span_db": 34.2, "learned_from": 37, "dsp_ms": 0.373,
 "bins": [{"hz": 5099999995, "in_band": false, "mean_dbfs": -80.5, "max_dbfs": -63.1, "noise_dbfs": -87.4, "busy": 0.145, "strong": 0.005},
          ...
          {"hz": 5174999995, "in_band": true, "mean_dbfs": -28.9, "max_dbfs": -16.8, "noise_dbfs": -35.3, "busy": 0.21, "strong": 0.21},
          ...],
 "wifi": [{"channel": 36, "mhz": 5180, "busy": 0.21, "strong": 0.21, "max_dbfs": -16.8},
          {"channel": 40, "mhz": 5200, "busy": 0.14, "strong": 0.12, "max_dbfs": -22.3}]}
```

| option | default | |
|---|---|---|
| `--seconds S` | 1 | how long the report covers |
| `--per-second N` | 100 | snapshots a second, about; beside a 160 Msps stream the radio gets to about 68, beside 320 Msps to about 40, and the report then takes that much longer |
| `--threshold DB` | 6 | a bin is busy when it is this far above its noise in that snapshot |
| `--keep` | | leave it running; the next `occupancy` then answers with the next report |

How to read it:

- A snapshot is one 8 KiB chunk of the stream, 2048 samples: 51 us at 40 Msps, 6.4 us at
  320 Msps. `busy` is the share of the snapshots in which the bin was above the threshold, so it
  is a share of the time for anything longer than a snapshot.
- `busy` is energy, and near a strong transmitter its skirts make bins all over the span busy
  while it sends. `strong` is the share in which the bin was busy and within 20 dB of the
  strongest busy bin of that snapshot: where transmissions sit. `wifi` gives both for each 20 MHz
  Wi-Fi channel, as its busiest bin has them.
- Levels are dBFS (0: a tone that fills the converter) and as sampled. With the AGC on, the
  receiver's gain moves with what is in the air (`gain_span_db`: how far the noise moved over the
  report), so a level is not a field strength; `busy` and `strong` do not depend on the gain.
  With a fixed gain (`w1700k gain --lna .. --lpf ..`) levels compare across snapshots.
- The samples come at twice the channel filter's width, so only the middle half of the bins is
  inside the filter (`in_band`); the outer ones show the filter's skirts, and at low gain their
  noise is under the converter's last bit, so little there means anything. `wifi` lists the
  channels that lie inside the filter whole: at 160 Msps that is an 80 MHz span.
- The noise is found by the radio, not set: `noise_dbfs` is what it took each bin's noise to be.
  Its shape over the bins is what the quiet moments share: the radio keeps the first 64
  snapshots after being turned on (they are in the first report, which therefore takes that long
  at the least) and takes the shape from the largest group among them that are alike but for the
  gain: `learned_from` of the 64. Under 8, the band was hardly ever quiet over this span: the
  report says so in `note`, its shares are not to be trusted, and with `--keep` the radio goes on
  looking, 64 snapshots at a time. Seen at 2.4 GHz in a flat with many networks: 30 of 64 quiet
  over 40 MHz, 3 to 6 over 160 MHz; a narrower span sees more quiet.
- What it cannot see: a signal that fills more than seven eighths of the span reads weaker than
  it is, and what is there all the time, the same, is noise to it.
- A firmware set built before the DSP image existed has none: the command then says so, and
  nothing is touched. `make -C firmware`, then `w1700k load`.

Measured on a unit: the stream lost no slice with it on at 40, 160 and 320 Msps; its shares
agreed within 0.04 in every bin (0.02 on average) with the same rule worked out on the host from
every chunk of the same six seconds; and on 6 GHz with nothing in the air no bin was busy.

## The low-power radio

The W1700K carries a second radio, a Silicon Labs EFR32MG21 for Bluetooth LE
and Zigbee/Thread, which the unit holds in reset unless its owner turns it on
([`platform`](../../platform/README.md#the-low-power-radio); a guide to it and
the GPS receiver: [docs/ble-zigbee-and-gps.md](../../docs/ble-zigbee-and-gps.md)). `w1700k efr32`
is that radio from the host. It involves no receiver state and starts no
controller.

| | |
|---|---|
| `efr32 status` | whether it runs (`efr32`), what start-up does (`start`), whether its port is there (`port`) |
| `efr32 on [--keep]`, `efr32 off [--keep]` | let it run or hold it in reset, until the unit next starts; with `--keep`, at every start-up too |
| `efr32 identify` | ask the radio what firmware it runs |
| `efr32 pty [--baud N] [--name PATH]` | its serial port as a terminal device on this host, until stopped |

The port is there only at a boot where the radio is kept on, so the first time
is `w1700k efr32 on --keep`, a restart of the unit, then `identify`:

```
$ host/bin/w1700k efr32 identify
{"firmware": "bgapi", "stack": "6.2.0", "build": 212, ...}
```

- **`identify`** sends the two commands of Silicon Labs' BGAPI that every
  Bluetooth co-processor image answers and that make it do nothing else
  (`system_hello`, `system_get_version`). The factory firmware is such an
  image. Other firmware will not answer, and that is said.
- **`pty`** prints a device path (`/dev/pts/N`) and stays: that device is the
  radio's port, at 115200 baud (the factory firmware's), 230400 or 460800, 8N1
  without flow control, carried over ssh. Point a Bluetooth host stack, a
  flasher or a terminal at it. The link adds a few milliseconds each way.
  With `--name PATH` it makes a link to the device at that path for as long as
  it runs and prints that instead: a name that stays the same from run to run.
- **What goes through the port is the user's doing.** A host that tells the
  radio to advertise, scan or connect makes the unit transmit at 2.4 GHz, up to
  10 dBm, beside its own 2.4 GHz antennas. Nothing here does that.

## The GPS receiver

The W1700K carries a GPS receiver (an Airoha AG3352), which the unit keeps
switched off unless its owner turns it on
([`platform`](../../platform/README.md#the-gps-receiver)). `w1700k gps` is
that receiver from the host. It involves no receiver state of ours and starts
no controller.

| | |
|---|---|
| `gps status` | whether it runs (`gps`), what start-up does (`start`), whether its port is there (`port`) |
| `gps on [--keep]`, `gps off [--keep]` | switch it on or off, until the unit next starts; with `--keep`, at every start-up too |
| `gps check [--seconds N]` | listen: does it talk, and has it a fix |
| `gps pty [--baud N] [--name PATH]` | its serial port as a terminal device on this host, until stopped |

The port is there only at a boot where the receiver is kept on, so the first
time is `w1700k gps on --keep`, a restart of the unit, then `check`:

```
$ host/bin/w1700k gps check
{"sentences": {"GGA": 3, "GSA": 12, "GSV": 16, "RMC": 3, ...}, "bad": 0, "fix": "3d", "satellites": 7}
```

- **`check`** reads what the receiver sends for a few seconds and says what
  kinds of sentence came, how many lines were not sentences, whether there is
  a fix and from how many satellites. It never says where: the position is
  not looked at. It writes nothing to the receiver.
- **`pty`** prints a device path and stays: that device is the receiver's
  port, NMEA 0183 at 921600 baud, carried over ssh, and what is written to it
  goes to the receiver. Any program that reads a serial GPS opens it as one.
  With `--name PATH` the device has a name that stays:

  ```
  $ host/bin/w1700k gps pty --name ~/w1700k-gps &
  $ gpsd -N -n ~/w1700k-gps           # or: pygpsclient, with ~/w1700k-gps as its user-defined port
  ```

  Seen with PyGPSClient, the name as its user-defined port at 921600: a 3D
  fix and every sentence parsed. gpsd has not been run against it.
- **Where the unit is, is its owner's to know.** Nothing in this package or on
  the unit stores or uses what the receiver says; it goes where the owner's
  programs take it.

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
| `{"id":10,"op":"occupancy","seconds":1,"per_second":100,"threshold_db":6,"keep":false}` | one occupancy report of the running stream, in the reply's `occupancy`; every field optional |

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
