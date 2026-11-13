# w1700k_sdr

Linux driver and unit-side tools that run the W1700K's MediaTek MT7996
(PCI `14c3:7990`, primary interface) as an SDR receiver. The driver:

- loads the MCU firmware (ROM patch, then the WM, WA and optionally DSP RAM
  containers) over the WFDMA download rings;
- turns the WM event ring into a stream ring: 1536 RX descriptors cycling
  through a 1 GiB DRAM buffer of 8 KiB slices, mapped read-only by userspace;
- optionally sends every slice to the network as one UDP datagram, zero copy;
- relays small word writes/reads to the firmware's mailbox page, also while a
  stream runs.

It runs our own firmware (`firmware/`), which takes its commands in that
mailbox page; the driver sends no runtime MCU commands. Three WFDMA rings are
bound: TX 16 (firmware scatter chunks), TX 17 (download commands) and RX 0
(download replies, then the stream).

Everything is polled; the driver requests no interrupt. It disables PCIe ASPM
(L0s/L1) on the device at probe, since ASPM limits device DMA to about
0.33 GB/s and the stream needs 1.28 GB/s.

Files: `pci.c` (device, ioctls, mmap), `pump.c` (stream pump), `export.c`
(UDP export), `fwdl.c` (download executor), `fwdl_wire.c` (download command
wire format), `fwdl-image.c` (container parsing), `dma-ring.c` (rings),
`wfdma.c` (WFDMA control), `mmio.c` (BAR translation), `ownership.c`. The
clients are `probe.c`, `download.c`, `stream.c` and `ctl.c`, with
`client.h`. The ABI is `include/uapi/w1700k_sdr.h`.

## Build

```sh
# module, against a configured and built kernel tree
make -C platform/linux module KDIR=/path/to/kernel/build CROSS_COMPILE=aarch64-linux-musl-
# the four static clients
make -C platform/linux reader CROSS_COMPILE=aarch64-linux-musl-
```

Both write to `kernel/w1700k_sdr/build/`. Without arguments both use the
platform build (`platform/build/linux` and its Buildroot toolchain), as
`make sdr` at the top level does. The clients are C11, one source file each,
and must come from the same tree as the module: every client checks
`W1700K_SDR_ABI_VERSION` and the capability bits it needs and refuses to run
against a different driver (`mt7996-download --reset` excepted, so a new
client can reset an old driver before it is unloaded).

Host tests (C harnesses compiled with the host compiler, no hardware):

```sh
python3 -m unittest discover -s tests -p 'test_kernel_*.py'
```

## Device

`/dev/w1700k-sdr`, mode 0600. Every ioctl except `GET_INFO` needs
`CAP_SYS_RAWIO`. `read()` returns one JSON status line without touching the
device, also during a stream:

```json
{"abi":5,"driver":"w1700k_sdr","pci":"0000:01:00.0","online":true,"suspended":false,"dma_exposed":true,"streaming":false}
```

## Ioctls

| ioctl | what it does |
|---|---|
| `GET_INFO` | ABI version, capabilities, PCI identity, `max_read`, BAR0 size |
| `ACQUIRE_HOST` | band-0 host ownership (LPCR DRV_OWN, wait FW_OWN_STAT clear, ack). Needed by every device access; lost on `RESET_MT` |
| `DOWNLOAD` | one firmware container, format detected: the ROM patch (semaphore, regions, PATCH_FINISH) or a RAM container (regions, FW_START with the RAM type's working-PDA bit). The first call allocates and binds the rings, enables bus mastering and pins the module |
| `RESET_MT` | WiFi subsystem reset (`0x70028600` bit 0 pulsed, 20 ms per edge), then frees the rings and unpins the module. Refused while the stream ring is mapped or exported datagrams are queued |
| `READ_MEM` | diagnostics: up to 4096 bytes through the BAR translation, only inside the host CSR page `0x000e0000-0x000e0fff` (LPCR `0xe0010`, FW_STATE `0xe00f0`), ROM `0x00800000-0x0080ffff` or patch SRAM `0x00900000-0x0090ffff` |
| `STREAM_RX` | the stream pump, on the calling thread (below) |
| `CTL` | up to 64 word writes then 64 word reads in `0x0090b000-0x0090fffc`; performed by the pump between sweeps while a stream runs |

## Clients

All print JSON lines on stdout and exit 0 on success, 1 on failure, 2 on a
usage error. Lines marked `attempt` are flushed before the device is touched.

```
mt7996-probe DEVICE ADDRESS LENGTH [--acquire] [--raw FILE]
  {"at":"attempt","address":"0x...","length":N}
  {"at":"read","pass":1,"ok":true,"word0":"0x...","word1":"0x...","last_word":"0x...","nonzero_bytes":N,"allones_words":N}
  FW_STATE is `mt7996-probe DEVICE 0x000e00f0 4` (word0; 0x7 = running).

mt7996-download DEVICE IMAGE --i-understand [--wa | --dsp]
  {"op":"download","image":"...","bytes":N,"container":"ram|patch|none","ram_type":"wm|wa|dsp",
   "start_sent":bool,"sem_get":N,"sem_release":N,"ioctl_rc":N,"errno":N,"errno_text":"...",
   "phase":N,"phase_name":"...","executor_result":N,"region":N,"offset":N,
   "tx_submitted":N,"tx_completed":N,"replies":N,"last_fw_state":"0x..."}
mt7996-download DEVICE --reset
  {"op":"reset","ioctl_rc":N,"errno_text":"...","was_dma_exposed":N,"rings_released":N,
   "stop_observe":N,"fw_state_after":"0x...","owner_after":"0x...","fw_own_stat_after":N}

mt7996-stream DEVICE --seconds S [--idle-ms N] [--go ADDR VALUE]
              [--stop ADDR VALUE] [--threads N] [--touch]
              [--data BYTES FILE] [--wrap] [--meta FILE] [--snap SLICES FILE]
              [--export IF SRC_IP DST_IP DST_MAC PORT] [--ports N]
              [--drain-ms N] [--export-cpu N] [--cpu N]
              [--send SRC_IP DST_IP PORT] [--format 16|14|8|7] [--piece BYTES]
              [--datagram BYTES] [--senders N] [--split]
  One {"at":"stream",...} line at the end (see stream.c for the fields).

mt7996-ctl DEVICE [--timeout MS] [ADDR=VALUE ...] [--read ADDR COUNT]
mt7996-ctl DEVICE --serve
  {"at":"ctl","ok":bool,"errno":"...","result":N,"via_pump":0|1,"writes":N,
   "read_address":"0x...","read":["0x...",...]}
  --serve takes one request per stdin line and answers each with one line.
```

## Bring-up

Our firmware set (`make -C firmware`, `firmware/build/firmware.json`), as
`w1700k load` downloads it:

```sh
D=/dev/w1700k-sdr
mt7996-probe $D 0x00901000 4 --acquire           # host ownership
mt7996-download $D --reset                       # clean MCU state
mt7996-probe $D 0x00901000 4 --acquire           # ownership again after the reset
mt7996-download $D w1700k_rom_patch.bin --i-understand
mt7996-download $D w1700k_wm.bin --i-understand
mt7996-download $D w1700k_wa.bin --i-understand --wa
mt7996-probe $D 0x0090f300 4                     # word0 0x4d57574f ("OWWM"): our WM's status block
```

Our WM is then driven through its mailbox page with `mt7996-ctl`
(`host/w1700k/wm.py`, `firmware/wm/wm.h`). Before `rmmod`, run
`mt7996-download $D --reset`: the module stays pinned while the device can
reach the rings.

## Streaming

`STREAM_RX` with `timeout_ms` 0 returns the geometry. Map the device with
`PROT_READ`, `MAP_SHARED`, offset 0 and length
`header_bytes + slices * slice_bytes`:

```
struct w1700k_sdr_stream_ring   (header_bytes, page aligned)
  u64 completed                 completions so far (load-acquire)
  u32 slots, slices, slice_bytes, header_bytes, reserved[10]
  u32 ctrl[slices]              per slice: descriptor ctrl, length in [29:16]
slice 0 .. slices-1             slice_bytes each
```

Completion `c` is in slice `c % slices`. A reader more than `slices - slots`
completions behind `completed` has been overwritten. Command replies also
pass through this ring, so completion numbers do not start at 0.

A stream: the host posts the STREAM radio command to our WM through `CTL`
(`host/w1700k/stream.py`); the WM arms the capture and enters its producer,
which waits for the go word. Then, as `stream.py` runs it:

```sh
mt7996-stream $D --seconds 604800 --idle-ms 300 \
    --go 0x0090f008 0x474f474f --stop 0x0090f008 0x504f5453 --threads 0 \
    --export eth1 10.77.0.1 10.77.0.10 aa:bb:cc:dd:ee:ff 7700 --ports 8 \
    --drain-ms 3000 --export-cpu 2 --cpu 3
mt7996-ctl $D --read 0x0090f000 64                     # the producer's result page, afterwards
```

The driver, already holding the device, writes the go word. The stream ends
at `--seconds`, on SIGINT/SIGTERM/SIGHUP,
after `--idle-ms` without data, on device removal or on a ring error. In every
case the driver then writes the stop word and keeps re-arming the ring until
`max(idle_ms, 300 ms)` pass without a completion (at most 5 s), so the
producer can drain its own TX ring. `kill -USR1` takes a `--snap` snapshot.

### Export datagrams

One per completed slice, from `src_port + n % ports` to `dst_port`, IPv4 with
DF, UDP checksum filled by the MAC:

| bytes | content |
|---|---|
| 14 | Ethernet: `dst_mac`, the interface's address, `0x0800` |
| 20 | IPv4 |
| 8 | UDP |
| 16 | `struct w1700k_sdr_export_header`: `le32 magic 0x31524453` ("SDR1"), `le16 offset` (0), `le16 slice_bytes` (this completion's length), `le64 seq` (completion number) |
| n | the slice's bytes |

The interface needs scatter-gather and an MTU of at least `slice_bytes + 44`
(8236 for 8 KiB slices). The device's TX queue is the only flow control; the
export reports `-ENOBUFS` if it falls more than `slices - slots - 64`
completions behind.

### The client's own senders

`mt7996-stream --send` sends the slices itself, from threads that follow the
mapped ring, through ordinary UDP sockets (the driver's export stays off). It
is for what the zero-copy export cannot do:

- `--format 16|14|8|7`: each raw slice requantised to that many bits per I/Q
  pair with a scale of its own (`include/hw/mt7996/iq_reduce.h`, NEON), under
  the magic `0x32524453` ("SDR2"): the same header, with `seq` in bits [47:0]
  of its last field, the format in [55:48] and the scale code in [63:56]. A
  slice is then 4096, 3584, 2048 or 1792 bytes. Events and packed slices leave
  unchanged under "SDR1".
- `--datagram BYTES`: a datagram carries as many whole slices as fit that much
  UDP payload, each behind its own header (two 14-bit slices in a 9216-byte
  MTU), which halves the cost of the network stack.
- `--piece BYTES`: every slice as datagrams of at most that many of its bytes,
  `offset` rising, for a path without jumbo frames.

The senders take completions in turn, a few at a time (`--senders`, default
2: two read more between them than three do). With `--cpu` the pump keeps its
CPU: the senders and the network device's NAPI threads stay off it while the
stream runs. Measured with the capture at 320 Msps, the SoC's memory bus lets
one sender read 0.60 GB/s of slices, two 1.0 and three 0.93, so the senders
carry 160 Msps (0.64 GB/s; all four formats, no slice lost, 20–45 % of each
sender's core idle) and not 320 (1.28 GB/s); reading further ahead of the
device than the slice in hand slowed the pump until the firmware overran.
In pieces, every rate from 80 to 239 thousand datagrams a second was clean
with the packets counted at the port (200 and 239 thousand for two minutes);
five runs in a row at 235 thousand once had the port transmit 0.1–0.2 % fewer
than the send calls accepted, for a reason not found.
A batch is four slices: what a sender has reduced must still be in the
512 KiB L2 the four cores share when the kernel copies it out (batches of 16
cost the 160 Msps case 15–25 % of each sender's core). Senders the ring
laps skip to the newest completions and count what they skipped (`"send"` in
the result line: `slices`, `reduced`, `datagrams`, `lapped`, `errors`, `busy`,
and `max_lag`, the furthest behind the device a sender took a completion).

### Split streams

At 320 Msps neither path carries a reduced stream alone, so `--split` runs
both (`--export ... --send ... --format 14 --split`) on a stream whose radio
reduces 10 of every 16 chunks itself (the firmware's split schedule):

- The export is opened with `W1700K_SDR_EXPORT_SHORT` in its `flags` (the
  field that was `reserved`): it sends only completions shorter than a slice,
  zero-copy as ever, and skips the raw ones. Its thread then sleeps 20–50 us
  when it has nothing to send instead of polling, so it needs no CPU of its
  own; the client lets it run on every CPU but the pump's and gives it
  priority (it must run when it wakes, or the radio's chunks trail the
  others). A module without the flag refuses the stream with `EINVAL`.
- The senders take only the raw slices and reduce them to format 14.
- A third thread keeps the radio's shift (the mailbox word at `0x0090f00c`,
  written through `W1700K_SDR_CTL`, which the pump serves between sweeps)
  wide enough for twice the largest magnitude the senders saw in the last
  second, rising within a millisecond and falling after a second
  (`"split"` in the result line: the writes, and the lowest, highest and last
  shift).

Each sender has a CPU to itself where there are enough (4 CPUs: the pump on
3, the senders on 1 and 2, the kernel's network threads on 0), and while a
stream runs the senders and those network threads have priority over
ordinary processes (nice -10; the pump and the export thread -20). It
matters. Sharing their CPUs with the network threads, the senders lost them
for 2–12 ms some 26 times a second each, and with little room to make that up
they ran 10 ms and more behind 47 % of the time and up to 0.21 s; on their
own CPUs 94 % of their work is taken within a millisecond. The export thread
may run on the senders' CPUs too (short bursts, at priority): confined to the
one CPU everything else shares it fell half a second behind when that CPU
was busy and did not catch up. And at ordinary priority two busy loops on
the unit cost a 320 Msps stream a fifth of its slices while they ran; with
priority, none, the export 36 ms behind at worst.

Measured at 320 Msps on a 5 Gbit/s link (4.6 Gbit/s on the wire, a USB
adapter on the host) for five minutes: 47.7 million chunks, 29.8 million
exported and 17.9 million reduced by the senders, none skipped, none lost at
the host; the export at most 35 ms behind the device and the senders 32 ms,
against the receiver's wait of 250 ms; the WM 77 % busy, the WA 87 %, the
SoC's three other CPUs each about 70 %. (Ten minutes before the senders had
their own CPUs: 94.4 million chunks, none lost, but up to 0.14 s and 0.21 s
behind.) The radio taking 8 or 9 chunks of 16 instead of 10 leaves the
senders more than they can reduce.

## Hazards

- The first `DOWNLOAD`/`STREAM_RX` after loading allocates 1 GiB as
  131072 order-1 pages (no CMA): load the module early, before memory
  fragments.
- The rings stay allocated and the module pinned until `RESET_MT`. Removing
  the device resets the WiFi subsystem before unmapping; if a mapping or a
  queued datagram still exists then, the module stays pinned.
- A download command whose TX descriptor is never reaped makes every later
  `DOWNLOAD` and `STREAM_RX` fail with `EBUSY` until `RESET_MT`.
- Host reads or writes inside the WiFi power island (UMAC SYSRAM, PLE/PSE,
  LMAC) can hang the SoC and wedge the MT7996 until power is removed; that is
  why `READ_MEM` and `CTL` accept only the windows above.
- A stream holds the device: other ioctls wait for it, except `GET_INFO`,
  `read()` and `CTL`.
- Lossless export needs the AN7581 QDMA congestion drop
  (`TXQ_CNGST_DROP_EN`) off; airoha_eth enables it.

## License

GPL-2.0-only. `fwdl_wire.[ch]` and `tests/mt76_vectors.h`
derive from mt76 (Copyright (C) 2022 MediaTek Inc.) and keep its
BSD-3-Clause-Clear license, see `LICENSE.mt76`. The uapi header is
`GPL-2.0 WITH Linux-syscall-note`.
