# w1700k-spectrum

A live spectrum and waterfall of the W1700K's sample stream in a web browser, with the
receiver's controls: run/stop, LO, band and sample rate, analog filter, gain and the stream's
roll-off correction. One process reads the stream as `w1700k-stream` serves it
([`../stream`](../stream/README.md): shared memory), computes the spectrum (FFTW, several
threads) and serves the page; receiver settings go to a controller process, by default
`w1700k serve` from the host package.

## Build

Needs a C++17 compiler, zlib and FFTW3 in single precision (Debian/Ubuntu:
`apt install g++ make zlib1g-dev libfftw3-dev`).

```sh
make                                  # build/w1700k-spectrum, the page compiled in
make CXXFLAGS="-O3 -march=native"     # tuned for this CPU
make install PREFIX=/usr/local
```

The default flags are portable (`-O3`). Warnings are errors; `make WARNINGS=-Wall` relaxes that.

## Run

```sh
../stream/build/w1700k-stream &       # the stream service (it receives the export)
build/w1700k-spectrum                 # then open http://127.0.0.1:8073/
```

The stream service may start before or after the server, and restart: the server follows it.
`w1700k` must be on `PATH`, or name the controller with `--controller`.

| option | default | |
|---|---|---|
| `--listen ADDR:PORT` | `127.0.0.1:8073` | the page; `PORT` alone listens on every address |
| `--web DIR` | built in | serve the page from DIR, read on every request (development) |
| `--stream NAME` | `/w1700k-stream` | the stream service's shared memory |
| `--fft-threads N` | 4 | FFT workers |
| `--controller CMD` | `w1700k serve` | the receiver's controller, run with `sh -c` |
| `--no-controller` | | a passive viewer: no receiver controls |
| `--center MHZ` | 5250 | the stream's centre frequency until the controller reports it |
| `--rate MSPS` | 320 | its sample rate until the controller reports it |
| `--dump-dir DIR` | `/tmp` | where SIGUSR1 writes a snapshot |
| `--dump-seconds S` | 0.3 | the snapshot's length |

One browser is served at a time: opening the page elsewhere takes over (the previous one is
closed with code 4001 and offers to take it back).

The FFT workers take the CPU that is left: they run at nice 15, so the stream service's receive
threads, this server's web thread and a browser on the same host come first. With "every sample"
averaging on a host that cannot transform them all, frames keep their rate and carry a share of
their blocks (`spectrum.share` in the status, and the coverage shown in the Stream panel). FFTW's
measured plan for each FFT size is found once in the background (seconds for the largest; the
estimated plan serves meanwhile) and kept in `$XDG_CACHE_HOME/w1700k/fftwf-wisdom` (default
`~/.cache`): at 262,144 points it transforms about 40 % more samples per core than the estimate.

`kill -USR1` writes the newest samples, as the stream serves them, to
`DIR/w1700k-dump-<unix ms>.bin` (complex int16, I then Q, on the 14-bit scale: full scale 8192)
and a `.json` with `center_hz`, `sample_rate`, `tuning_steady` (false if the tuning changed
within the snapshot), the slices covered, the slices missing (written as zeros) or holding
firmware events (left out), and `flattened_slices`, those the correction produced.

## The controller

The controller is a child process speaking newline-delimited JSON on its stdin and stdout:
requests `{"id":N,"op":...}` each get one reply `{"id":N,"ok":true|false,...}`; lines without
an `id` are events (`state`, `progress`, `log`). The server asks for `status` first, then sends
one request at a time. A controller that exits, closes its output, or does not answer in time
(configure 600 s, start/stop 120 s, others 30 s) is stopped and restarted after 1, 2, 5, 10,
then 30 s; the page shows it as down meanwhile, and requests that were queued are dropped.

| page control | request |
|---|---|
| run button | `start` or `stop` |
| LO: the header display (wheel, click, keys, typing), the LO box, double-click on the plot | `tune` `hz`, refused outside the band's `rf.lo_range_hz`; any time the radio is loaded |
| Band, Sample rate 40-320 Msps or 640/2 (band 2), then Apply | `configure` `band`, `sample_rate`, `capture_rate` (the LO stays) |
| Filter 20-320 MHz | `filter` `mhz` (and the filters stop following the sample rate) |
| Follow the sample rate (ticked by default, remembered in the browser) | `filter_follow` `on`: every band's filter as wide as its sample rate |
| Automatic gain control, LNA 0-4 (~6 dB steps), Baseband 0-9 (~2 dB steps) | `gain` `agc:true`, or `lna` and `lpf` |
| Flatten the passband | none: the stream service's `flatten` control word |

Flatten asks `w1700k-stream` to correct the stream itself for the decimation's roll-off, for every
consumer; the server also writes the radio's sample rate, capture rate and filter from the
controller's state, and the correction applies where the filter is as wide as the sample rate and
the rate is not 640/2 (`receiver.flatten_available`, `flatten_active` in the status).

The controller picks the stream's packing from the sample rate and the link's speed (raw where it
fits, else every chunk or every other chunk packed to 28 bits, lossless either way), unless it is
set by hand (`w1700k serve --packing`, or `export.packing` in its configuration). The header shows
what the stream carries next to the sample rate and band, for example `14-bit, packed
(28 b/sample)`, with `(manual)` when set by hand; the Stream panel shows the share of packed slices. While a configure,
start or stop runs, other requests are refused and the page
disables its controls and shows the controller's progress. Tune, gain and filter requests
queue one at a time; a newer one of the same kind replaces one not yet sent.

The controller's state says what the stream is: `stream.center_hz` and `stream.sample_rate`
label every frame from then on. A tune or configure marks the stream as changing from the
newest slice received; frames are dropped from there until the state reports the new tuning,
plus 1024 slices for datagrams still on their way. A frame never spans a change, so none
carries samples of one tuning under the label of another.

## The stream

The stream service ([`../stream`](../stream/README.md)) receives and orders the export; the
server reads its shared ring in slot order. A slot holding a firmware event takes no sample time
(the samples either side are contiguous) and a lost one leaves a gap; FFT blocks that would span
either are left out. The server asks the service for a new run when the controller reports the
stream stopped or is asked to start one.

## Page and server

HTTP serves the page; everything else is a WebSocket at `/ws` (permessage-deflate). Messages
are JSON text with a type `t`, plus binary spectrum frames.

From the page: `ping {c}`, `ack {seq}` for every frame, `view {start, stop, bins, auto, fps,
kbps}` (the range and resolution wanted, and pacing limits), `fft {size, window, rate,
averaging, peak, dc}`, `set {...}` with any of `lo`; `band, sample_rate, capture_rate`;
`filter`; `filter_follow`; `agc, lna, lpf`; `flatten`, and `run {on}`.

From the server: `hello {version, windows, fft_defaults, max_bins, controller, bands, rates {label,
sample_rate, capture_rate, bands}, filters, max_lna, max_lpf}`, `status` (sections `state`,
`message`, `running`, `devices`, `receiver {lo, sample_rate, flatten, flatten_available,
flatten_active}`, `controller {mode, command, alive, pid,
restarts, busy, progress, error, queued, state}`, `fft`, `spectrum`, `stream`, `link`; unchanged
ones are left out), `notice {level, text}` and `pong`. A frame is a 48-byte little-endian header
(`u8` kind 1, `u8` flags (bit 0: peak detector), `u16` bins, `u32` sequence, `f64` start and stop
Hz, `f32` base dB, `f32` step dB, `u32` frames merged, `u32` FFT blocks, `f64` server time ms)
and one byte per bin: dBFS = base + code × step.

## Tests

`python3 -m unittest discover -s tests -p test_spectrum_server.py` from the repository root
(`make check` runs it) builds the stream service and the server and runs them against a fake
controller (`tests/spectrum_fake_controller.py`) and a fake export over loopback.
