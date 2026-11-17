# w1700k-stream

The host's one receiver of the W1700K's UDP export. It puts the stream in order, unpacks it and
serves it, as complex int16 IQ, to every consumer on the host through shared memory:
`w1700k-spectrum`, `sdr-recv`, the SoapySDR module ([`../soapy`](../soapy/README.md)), or any program that
maps the ring. On request it also corrects
the stream for the radio's decimation roll-off ("flatten"), in the samples themselves, so that
every consumer gets the corrected stream.

## Build and run

Needs a C++17 compiler and FFTW3 in single precision (Debian/Ubuntu:
`apt install g++ make libfftw3-dev`).

```sh
make                                   # build/w1700k-stream
make CXXFLAGS="-O3 -march=native"      # tuned for this CPU
build/w1700k-stream                    # until Ctrl-C; consumers attach any time
```

| option | default | |
|---|---|---|
| `--bind IP` | `10.77.0.10` | the stream's destination address on this host (the 10G link) |
| `--port N` | `7700` | its UDP port |
| `--unit IP` | any sender | take the stream from this sender alone, the unit's address on the link (`10.77.0.1`) |
| `--rx-threads N` | 8 | UDP sockets and receive threads (`SO_REUSEPORT`; the unit spreads its source ports) |
| `--workers N` | 6 | threads that unpack and correct |
| `--ring-mib N` | 512 | the shared ring, a power of two (512 MiB holds 0.42 s at 320 Msps) |
| `--name NAME` | `/w1700k-stream` | the shared-memory object (`/dev/shm/w1700k-stream`) |
| `--report S` | 0 | a JSON status line on stdout every S seconds |
| `--share-irq-cpu` | | run also on the CPU that takes the interface's packets (see below) |
| `--deep-idle` | | let the host idle as deeply as it likes while a stream arrives (see below) |

The object is the user's who runs the service, and its consumers run as that user: it is made
readable and writable by its owner alone, and a consumer opens only an object that is its own
user's, that nobody else may write, and whose layout lies inside it. On a host with other users
that keeps one of them from making an object under the name first and being read as the radio.
The service says so when the name is held by somebody else; `--name` gives it and its consumers
another.

Where one CPU takes every packet of the interface that holds the bind address (an interface
with a single receive queue, such as a USB adapter: the CPU its interrupt is delivered to), the
service keeps its threads off that CPU, looks again every two seconds, and names the CPU in the
shared header (`receive_cpu`) for its consumers; `w1700k-spectrum` follows it. That CPU has
nothing to spare at 320 Msps, and what the adapter cannot hand over in time it drops
([`tools/r8152-rtl8159`](../../tools/r8152-rtl8159/README.md) has the measurements).

While datagrams arrive, and for two seconds after the last, the service asks the kernel to keep
the CPUs out of their deep idle states (a CPU latency request of 100 us, held through
`/dev/cpu_dma_latency`). A stream at 40 or 80 Msps arrives in clumps, and a host that goes to
sleep between them can be too slow to wake for a USB adapter, which holds under a millisecond
of stream: 0.3 to 0.9 % of the 16-bit format's slices at 40 Msps on one such host, next to none
with the request ([the measurements](../../tools/r8152-rtl8159/README.md)). That file is root's
until `init/host/link` gives it to the user; where the service may not open it, it says so once
and carries on. Between streams the host idles as it likes.

The host needs the destination address on the 10G link, an MTU of at least 8236
(`sudo init/host/link keep IFACE 10.77.0.10/24 9216` gives the interface both and keeps them
there; an interface with a smaller MTU drops the stream without a word), and room for
48 MiB socket buffers (`sysctl -w net.core.rmem_max=67108864`, or the kernel caps them silently
and drops datagrams under load: counted as `socket_drops`). One service owns the port: a second
receiver of the same port would take half the datagrams, so consumers read the ring instead. A
second service under the same name refuses to start; at exit the object is unlinked.

## The stream

A stream reduced to fewer bits (`w1700k` packings `16`, `14`, `8`, `7`) comes under the magic
`0x32524453` "SDR2" instead: the same header, with `seq` in 48 bits, the format (bits per I/Q
pair) in the next 8 and the slice's scale code in the top 8; the slice is 4096, 3584, 2048 or
1792 bytes (`include/hw/mt7996/iq_reduce.h`), and several whole slices may share a datagram,
each behind its own header. The service scales them back to 14-bit samples, so consumers see no
difference but the quantisation noise (`reduced` counts them). A slice may also come in pieces
(`export.piece`): datagrams with a rising `offset`, assembled here; a slice with a piece missing
counts as lost, and `pieces` counts those.

In a split stream (`w1700k` packing `split`, 320 Msps on 5 Gbit/s) the radio reduces most of
the chunks itself: those come under "SDR1" as completions of 3588 bytes whose last word is
`0x524431xx`, the low byte the shift their samples were cut under
(`include/hw/mt7996/iq_format.h`); the chunks between come reduced by the unit under "SDR2".
Both are scaled back to 14-bit samples and counted in `reduced`. The radio is told its shift
from the levels the unit sees in the chunks between, so a level that more than doubles within
a millisecond can wrap in the radio's chunks until the shift follows: `suspect` counts radio
chunks next to a unit-reduced slice whose peak was beyond their shift's range. They are
published all the same.

The export sends one UDP datagram per completion of the unit's DMA ring: a 16-byte header
(`u32` magic `0x31524453` "SDR1", `u16` offset, `u16` slice_bytes, `u64` seq) and one slice, 8192
bytes raw (2048 samples, I in bits [29:16], Q in [13:0]) or 7168 bytes packed (of each 8 samples,
the 8th rides in the other 7's spare bits [31:30] and [15:14]); completions shorter than 4096
bytes are firmware events that share the ring.

Completions are filed by `seq` into a reorder ring (256 MiB, 0.2 s at 320 Msps). A run, one stream, begins with the
first datagram after a second of silence, after a jump in `seq`, or after a consumer asks for one
(`restart_requests`, which the spectrum server does when the receiver starts or stops). Slots are
published in order: one whose completion has arrived, a firmware event, or a loss: a slot that is
still missing 250 ms after 1024 newer completions arrived (a receive thread kept off its CPU on a
busy host delivers late, not never, and the two paths of a split stream run tens of milliseconds
apart at moments), or at once when half the reorder ring is newer (`--receive-mib`, default
1 GiB: 0.42 s at 320 Msps). After 5 ms with nothing new, what has arrived is
published; a loss at the very end of a stream stays unpublished until more arrives. Of the
losses, `overruns` counts those that did arrive but were overwritten in the reorder ring before
the service published them: the service fell behind (CPU contention), not the export.

Every completion is checked on the way in, for the export's integrity: words of raw slices with
spare bits set (`spare_words`), and slices or 8-sample lines (28 bytes packed, 32 raw) equal to
the same slice or line one lap of the unit's capture ring (128 completions) earlier
(`stale_slices`, `stale_lines`), which a reader that overtook the capture, or a packer reading a
stale cache line, would send. Flat lines (all eight samples equal, which the radio does output)
are not compared.

## The shared memory

[`include/w1700k_stream.h`](include/w1700k_stream.h) is the interface, for C and C++: the
layout, the tag rules and helpers to map the object and read the position. In short:

- A 4 KiB header: the layout, the published position (`run`, `newest`: every slot of the run below
  `newest` is final), counters, a heartbeat (CLOCK_MONOTONIC, every 100 ms), and control words
  that consumers write.
- One 64-bit tag per slot: `(run << 40) | (index + 1)`, `index` counting the run's completions from
  0, plus a kind: `TAG_EVENT` (no samples, no sample time: the slices either side are contiguous),
  `TAG_LOST` (no samples; the sample time is lost), `TAG_FLAT` (corrected samples).
- Per slot one slice: 2048 complex samples, int16 I then int16 Q, on the scale of the radio's
  14-bit samples (full scale 8192). Uncorrected samples are the radio's, bit for bit; the int16
  range above them is the correction's headroom (a sample beyond it saturates, counted).

A reader reads a slot's tag (acquire), its samples, then the tag again: a changed tag means the
slot was overwritten meanwhile.

## The correction ("flatten")

Every sample rate's decimation attenuates the band's edges, signal and noise alike: about -2 dB
at 62 % of Nyquist, -13 dB at 87 %, -22 dB at 99 % (research ledger D293). The correction is a
129-tap linear-phase FIR (Kaiser window) whose response is the inverse of that roll-off as
measured on unit A, per rate, capped at +24 dB; it is applied zero-phase (every corrected sample
keeps its index) by overlap-save FFTs, across slice boundaries, with events skipped and losses as
zeros. Its response is within 0.15 dB of the inverse up to 0.95 x Nyquist.

It applies when a consumer asks for it (`flatten` = 1) and the radio's state, which the consumer
also writes (`radio_sample_rate`, `radio_capture_rate`, `radio_filter_hz`), says the analog filter
is as wide as the sample rate and the stream is not 640/2 (which is flat already); the service
reports what applies in `flatten_active` and `flatten_rate`. The spectrum server writes these
from the receiver's controller and the page's Flatten toggle.

Measured on unit A, band 1 at 320 Msps, from the receiver's noise floor (relative to 15..45 % of
Nyquist): without the correction -1.7 / -6.4 / -14.2 / -19.8 / -22.8 dB at 62.5 / 75 / 87.5 / 94 /
97 % of Nyquist; with it +0.9 / +0.5 / +0.1 / -0.4 / -1.1 dB. The service used 0.8 CPU cores
without it and 1.8 with it, with no loss.

## Tests

`python3 -m unittest discover -s tests -p test_stream.py` from the repository root (`make check`
runs it) builds the service and runs it against a fake export over loopback.
