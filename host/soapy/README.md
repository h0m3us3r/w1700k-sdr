# The SoapySDR module

The W1700K as a [SoapySDR](https://github.com/pothosware/SoapySDR) device, `driver=w1700k`, so
that programs written for SoapySDR can use it: GNU Radio (its Soapy source block), SoapySDRUtil,
and anything else that takes the unit's sample rates. One receive channel.

It adds no path of its own to the unit. The samples are the ones `w1700k-stream` serves to every
consumer on the host ([`../stream`](../stream/README.md): shared memory), and the settings go to
the receiver's controller, `w1700k serve` ([`../w1700k`](../w1700k/README.md)). So the spectrum
page, the recorder and a SoapySDR program read the same stream at the same time.

## Build and install

Needs a C++17 compiler and SoapySDR's headers (Debian/Ubuntu: `apt install libsoapysdr-dev`).

```sh
make                              # build/libw1700kSupport.so
sudo make install                 # into /usr/local/lib/SoapySDR/modules0.8, where SoapySDR looks
```

Or leave it where it is built and point SoapySDR at it:
`export SOAPY_SDR_PLUGIN_PATH=$PWD/build`.

The module starts the controller as `w1700k serve`, so `w1700k` must be on `PATH`, or name the
command: `export W1700K_CONTROLLER="/path/to/host/bin/w1700k serve"`. The stream service must be
running (`../stream/build/w1700k-stream`); if it is not and `w1700k-stream` is on `PATH`, the
module starts it and stops it again when the device closes.

```sh
SoapySDRUtil --find="driver=w1700k"
SoapySDRUtil --probe="driver=w1700k"
SoapySDRUtil --args="driver=w1700k" --rate=320e6 --direction=RX    # a rate test
python3 examples/gnuradio_capture.py --freq 5250e6                 # GNU Radio, through gr-soapy
```

In GNU Radio Companion: a *Soapy Custom Source* with driver `w1700k`.

## The device

| | |
| --- | --- |
| Sample rates | 40, 80, 160 and 320 Msps: the radio's own. Nothing lower: a program that cannot take 40 Msps cannot use the device yet |
| Formats | `CS16`, the native one: the radio's 14-bit samples as they are, full scale 8192. `CF32`: the same, full scale 1.0 |
| Antennas | `2.4G`, `5G`, `6G`: the unit's three radios (bands 0, 1, 2). One is captured at a time |
| Frequency | The LO, anywhere the radios lock: about 2.09–2.75 GHz and 4.65–7.58 GHz (the unit reports its own spans once loaded). A frequency the band in use reaches is a retune; one it does not reach switches to the radio that does (below 5925 MHz the 5 GHz one, above it the 6 GHz one). What is read back is the LO the synthesizer realised, within a few Hz |
| Gain | `LNA` 0–24 dB in steps of about 6, `LPF` 0–18 dB in steps of about 2, as measured; or the radio's automatic gain (the gain mode, on by default). Setting a gain ends the automatic one |
| Bandwidth | The analog filter: 20, 40, 80, 160 or 320 MHz, the narrowest that passes what is asked for. 0: as wide as the sample rate, and following it (the default) |
| Settings | `flatten`: the stream service corrects the decimation's roll-off, for every consumer of the stream. `capture_640`: the 6 GHz band at 320 Msps captured at 640 Msps, every other sample kept (flat to about ±155 MHz); the next sample rate or antenna set ends it |
| Sensors | `streaming`, `packing`, `link_mbps`, `synth_locked`, `error` from the controller; `service`, `slices`, `lost`, `late`, `overruns`, `reduced` from the stream service |

Device arguments, beside `driver=w1700k`:

| argument | default | |
| --- | --- | --- |
| `stream` | `/w1700k-stream` | the stream service's shared memory |
| `controller` | `$W1700K_CONTROLLER`, else `w1700k serve` | the controller's command, run with `sh -c`; `none` for a device that only listens: it reads the stream, and every setter is refused |
| `packing` | | set every stream's packing by hand (`w1700k serve --packing`) |
| `service` | `$W1700K_STREAM_SERVICE`, else `w1700k-stream` if on `PATH` | what to start when no stream service runs; `none`: nothing |

## How it behaves

- **Loading.** Opening the device asks the unit for nothing but its state. The first setter, or
  the first stream, loads the radio if nothing has yet (firmware and calibration, a few
  seconds).
- **The stream.** Activating a stream starts the unit's stream if it is not running, and
  deactivating the last one stops it again; a stream that was already running (the page's, say)
  is joined from that moment and left running. A sample rate change restarts the unit's stream;
  the reader carries on with the new one.
- **Times.** Every read carries a time (`SOAPY_SDR_HAS_TIME`), on the host's monotonic clock:
  the moment reading began, advanced by every sample since. They are sample counts turned into
  time, exact relative to each other; the unit has no clock of its own to offer.
- **Gaps.** Samples that never arrived, or that the ring overwrote before the reader came to
  them, are not papered over with zeros: the read returns `SOAPY_SDR_OVERFLOW` with the time at
  which the stream goes on, and the next read's samples follow from there. Firmware events in
  the stream carry no samples and are passed over. When a stream is deactivated the module
  logs what it read, the gaps, and how far behind the stream the reader ever was.
- **Keeping up.** A reader has the shared ring's length to fall behind in: 0.42 s at 320 Msps
  with the service's default 512 MiB. On a host that is also dropping datagrams the service
  publishes in bursts of a quarter of a second and more while it waits for late ones, which uses
  most of that; give the service a larger ring there (`w1700k-stream --ring-mib 1024`). Measured
  on an 8-core Intel N350: 20 s at 320 Msps in either format with no gap and the reader at most
  45 ms behind, with nothing else reading; with the spectrum page running too the host itself
  lost 0.02 to 0.06 % of the slices (as it does without this module) and the reader was up to
  0.36 s behind: at the edge of the ring, and in one run of several it went over it and lost a
  stretch of its own.
- **Other controllers.** The spectrum page and a SoapySDR program can both drive the
  receiver: a controller adopts what another changed within about two seconds
  ([`../w1700k`](../w1700k/README.md)), so the page follows a retune made here, and
  `getFrequency` and the rest report one made on the page. A sample rate changed elsewhere
  restarts the stream under a reader, which carries on at the new rate once the controller
  has reported it.

## Tests

`python3 -m unittest discover -s tests -p test_soapy.py` from the repository root (`make check`
runs it; it is skipped without SoapySDR's headers): the stream service fed by a fake export, a
fake controller, and the module driven through SoapySDR's API by `tests/soapy_check.cpp`, which
is also a handy way to try a unit:

```sh
c++ -std=c++17 -O2 ../../tests/soapy_check.cpp -lSoapySDR -o build/soapy_check
build/soapy_check driver=w1700k state freq=2442000000 read:CS16:320000000:-
```
