# sdr-recv

Records the W1700K's IQ stream as `w1700k-stream` serves it to every consumer on the host
([`../stream`](../stream/README.md)): complex int16 on the radio's 14-bit scale, corrected for
the decimation's roll-off where the service applies it. It needs only a C11 compiler: `make`
builds `build/sdr-recv`. Licensed GPL-2.0-only.

```sh
sdr-recv --seconds 10 --out stream.ci16    # the next run from its start, or the current one from now
sdr-recv --seconds 600                     # count only, any length
```

| option | default | |
|---|---|---|
| `--stream NAME` | `/w1700k-stream` | the service's shared memory |
| `--seconds S` | 10 | stop S seconds after the first slice |
| `--idle-ms I` | 1000 | stop after I ms without a new slice |
| `--wait S` | 120 | give up if no stream comes |
| `--out PATH` | | keep the samples; without it, count only |
| `--capacity-mib M` | 4096 | with `--out`: memory for the recording, allocated up front |
| `--report S` | 10 | a progress line every S seconds |

It follows one run in slot order and also stops when the run ends. With `--out`, PATH holds
every slice, 2048 samples each (int16 I then int16 Q): a lost slice as zeros, so that sample time
stays true, and a firmware event (no samples, no sample time) left out. PATH.json and the last
stdout line say what was kept: the run, its first slice, slices, samples, events, losses and
overruns (slots the ring overwrote before they were read, also zeros) with the first 64 of each,
the slices the correction produced, and why it stopped. A run of two channels
(`w1700k start --channels 2`) is recorded a sample time at a time: channel 0's I and Q, then
channel 1's, 1024 sample times to a slice, a lost slice as zeros in both; `channels` in
PATH.json says which a file is, and `samples` then counts sample times. The export's own integrity (spare bits,
data repeated a capture-ring lap later) is checked by the service, which counts it.
