# The stream link over USB: an RTL8159 adapter

A host without a 10 GbE port can take the W1700K's IQ stream through a USB adapter built on
Realtek's RTL8159 (USB 3.2 to 10GBASE-T, USB ID `0bda:815a`). The one this was developed with is a
Cable Matters 201498 (USB-C), about 80 US dollars; adapters with this chip sold from about 55
dollars in 2026 ([CNX Software, April 2026](https://www.cnx-software.com/2026/04/03/realtek-rtl8159-10gbe-to-usb-3-2-adapters-sell-for-about-55-and-up/)).

Everything below was measured on one host: an 8-core Intel N350 mini PC running Linux 7.0, the
adapter on a 10 Gbit/s USB port (USB 3.2 Gen 2, one lane), cabled to a W1700K2's `wan` port.

## What works

| | |
| --- | --- |
| Link | **5 Gbit/s**. The adapter offers 10GBASE-T but does not train at it here: three attempts, then both ends settle on 5 Gbit/s, 18 s after the unit's port starts (the front LED breathes yellow meanwhile). |
| Jumbo frames | MTU 9216 with this driver; the stream needs at least 8236. |
| 40 and 80 Msps | raw 14-bit samples, nothing lost |
| 160 Msps | every slice packed to 28 bits, lossless (4.5 Gbit/s), nothing lost |
| 320 Msps | the `split` packing, 14 bits per sample pair (4.6 Gbit/s): **0.00023 % of slices lost** over 27 minutes and 0.00003 % over a night of 9 hours, with the spectrum page open in a browser, the link set up as below and the host at 15 W (0.04 % before any of it) |
| Reduced formats | 16, 14, 8 and 7 bits per sample pair, up to 160 Msps |
| 10 Gbit/s | not reached; see "The missing firmware" |

`make sweep` in the repository's root runs every one of these on a unit and says which pass
(30 of 30 here); `make bench` watches the stream over minutes and says what the host lost and
under what load.

## The driver

A kernel whose `r8152` driver does not know the RTL8159 (7.0, on this host) hands the adapter to
`cdc_ncm`, which works but caps the MTU at 1504: no jumbo frames, so no stream without
`export.piece`. This directory builds Linux v7.3's `r8152.c`, unmodified, for the running kernel:

```sh
make                 # r8152.ko, for the running kernel (needs its linux-headers); not as root
sudo make install    # into /lib/modules/KVER/updates/, ahead of the kernel's own
```

Then plug the adapter in again.

Repeat after a kernel update (`make KVER=...` builds for another installed kernel), until the
kernel's own `r8152` knows the adapter; `sudo make uninstall` removes it.

`r8152.c` is `drivers/net/usb/r8152.c` of Linux v7.3 (SHA-256
`9d766f203c7fdabe6449e8dd2b9e5dd4fccfa20a62aa5dbf00d64c0c475c5a9f`), under the kernel's
GPL-2.0-only licence like the Makefile beside it; the rest of this repository is MIT.

## The missing firmware

The driver asks for a patch for the adapter's PHY, `rtl_nic/rtl8159-1.fw`, and says so in the
kernel log when it is not there:

    r8152 2-2:1.0: unable to load firmware patch rtl_nic/rtl8159-1.fw (-2)

linux-firmware did not carry that file in November 2026. Without it the driver still binds and
the adapter works, at 5 Gbit/s. Whether the patch is what 10GBASE-T training lacks has not been
shown: the same W1700K2 port links at 10 Gbit/s to its own second port and to another W1700K,
so the unit's side and its cabling are not what fails. Sellers also state that the full
10 Gbit/s needs a 20 Gbit/s USB port (USB 3.2 Gen 2x2); that was not tried here either.

## Setting the host up

```sh
sudo init/host/link keep IFACE 10.77.0.10/24 9216
```

That gives the interface the stream's address and a jumbo MTU, and spreads its packets over the
CPUs, now and whenever the adapter appears again (NetworkManager holds them in a connection
the command makes for the interface, with a hook beside it; see [`init/host/link`](../../init/host/link)).
All of these matter with this adapter:

- **The MTU.** Plugged in again, the adapter is back at 1500 and drops every stream packet
  without a word. The controller refuses to start a stream the interface would drop.
- **One CPU takes every packet.** A USB adapter has one receive queue, so all 127,000 datagrams a
  second of a 320 Msps stream arrive on the CPU its USB controller interrupts, by default the one
  most other interrupts share. The adapter can hold less than a millisecond of stream, and
  misses what arrives while that CPU is busy. Moving the interrupt to a CPU of its own and
  steering the IP and UDP work to the others took the misses from 53 datagrams a second to
  between 1 and 5; [`tools/host_rx_trial`](../host_rx_trial) measures each step on your host.
  `w1700k-stream` and `w1700k-spectrum` keep their own threads off that CPU.
- **A host that sleeps between packets.** A stream at 40 or 80 Msps arrives in clumps, and
  between them a host can put every CPU to sleep and then enter its deepest, whole-system idle
  state, which takes it too long to leave for an adapter that holds under a millisecond. On an
  arm64 tablet (CPU sleep state 1.3 ms to leave) the 16-bit format lost 0.3 to 0.9 % of its
  slices at 40 Msps and 0.1 to 0.35 % at 80, every one a datagram the adapter missed, while
  160 and 320 Msps, which keep the host awake, lost next to nothing. Any one CPU kept busy
  cured it (10 slices of 389,000). `w1700k-stream` therefore asks the kernel to keep the CPUs
  out of deep idle for as long as datagrams arrive, through `/dev/cpu_dma_latency`, which `link`
  gives to the user who ran it. With the interrupt moved but without that request: 0.25 % at
  40 Msps, 0.006 % at 80. With it: none of 1.17 million slices at 40 Msps, none of 2.34 million
  at 80, and `make sweep` 30 of 30 with none lost. `link` had not moved the interrupt on that
  host before: a USB controller that is no PCI device shows none in sysfs, and it is now looked
  up in `/proc/interrupts`. At 320 Msps that took the loss from 0.002 % to 2 slices of 18.8
  million in two minutes.
- **What is left** is the host: that one CPU is about 90 % busy with the adapter alone at
  320 Msps, on a machine whose eight cores were all occupied and clocked down to 1.7 GHz by its
  13 W power limit. At 160 Msps and below nothing was lost.
- **The host's power limit.** Those 13 W are the processor's short-term power limit as this
  machine's firmware leaves it (`/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw`),
  not a matter of temperature: the package was at 66 °C. Raised to 15 W, which this machine's
  cooling is good for, the cores ran at about 2.05 GHz and 71 °C, and a two-minute run at
  320 Msps lost 0.0008 % of slices, 0.6 datagrams a second, where the two before it lost 0.005
  and 0.006 %. One run, under the same desktop load; the setting lasts until the next reboot:

  ```sh
  echo 15000000 | sudo tee /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw
  ```

  Whether that is wise is a question of the machine's cooling, not of the stream.
- **A longer run, as the receiver is used.** `tools/host_bench.py` watched 320 Msps for 1599 s at
  15 W with the spectrum page open in a browser and nothing else heavy running:
  249.8 million slices, 566 lost (0.00023 %, 1 in 440,000), every one of them a datagram the
  adapter missed (284, or 0.18 a second) and none late or dropped in the host. Of 1539 samples
  of about a second, 131 had any loss, 100 of those a single datagram; one sample lost 76
  slices; the longest stretch with none was 79 s. Every core was still 92 to 95 % busy at
  2.05 GHz (the spectrum server 287 % of a CPU, the stream service 269 %), and the adapter's
  CPU 93 %, 81 % of it in the network stack.
- **Overnight.** The same watch for 33,758 s (9 h 23 min), again 320 Msps at 15 W with the
  spectrum page open: 5.27 billion slices, 1456 lost (0.00003 %, 1 in 3.6 million), every one
  of them a datagram the adapter missed (728, each costing two slices) and none late or
  dropped in the host. Of 32,837 samples 587 had any loss, 533 of those a single datagram; one
  sample lost 38 slices; the longest stretch with none was 775 s. The first two hours lost
  1 slice in 1.1 million and the seven after them 1 in 10 million. Nothing about the stream
  changed at that point; what changed on the host was not recorded (the desktop session, found
  locked in the morning, is the likely candidate). Every core was 87 to 93 % busy at 2.09 GHz
  throughout (the spectrum server 303 % of a CPU, the stream service 257 %).

Two things not to do while a stream is arriving: change the driver's ring (`ethtool -G`) or its
receive timer (`ethtool -C`), either of which left the adapter receiving nothing until the
interface was taken down and up (shortening that timer from 15 us to 1 us changed nothing in any
case); and expect the
link back within a second after a unit reboot (it takes the 18 s above).
