# Installing, updating and restoring a unit

`init/install` takes a W1700K running its stock firmware, backs up its NAND
data to the host, and installs the SDR system. `init/flash` updates an installed
unit from a new build. `init/restore` writes the backup back and returns the
unit to its stock firmware. `init/rawdump` takes a raw image of the whole NAND
for the maintainers. All of them drive the unit over its serial console and
serve images to its U-Boot over TFTP; none needs the unit's stock firmware to
work beyond its first-stage U-Boot.

## What you need

- The unit's serial console on a 3.3 V USB serial adapter, 115200 8N1.
- An Ethernet cable from a host NIC to the unit's `lan3` port.
- A Linux host with Python 3.10+, pyserial and OpenSSH (`ssh-keygen`), and the
  build outputs from `make` in the repository root.
- An SSH key pair; its public half is installed for root logins
  (default `~/.ssh/id_ed25519.pub`, `id_ecdsa.pub` or `id_rsa.pub`).

## Host setup, once

```sh
sudo init/host/link up IFACE                  # 192.168.1.10/24 on the NIC cabled to lan3
sudo init/host/tftp-forward enable IFACE      # stock U-Boot's UDP 69 -> port 7071
```

Stock U-Boot fetches files only from UDP port 69. `tftp-forward` installs a
systemd unit with one nftables rule that redirects exactly that traffic (from
192.168.1.1 to 192.168.1.10 on IFACE) to the installer's unprivileged port, so
the tools run as a normal user. Without it, the tools must run as root to listen
on port 69 themselves; root then also owns the files they write, under root's
home unless `--data-dir` and `--authorized-key` say otherwise.

For the 10 GbE IQ link (unit `eth1`, 10.77.0.1), give the host side its
address and jumbo frames: `sudo init/host/link keep IFACE10G 10.77.0.10/24 9216`.
`up` sets them until the interface goes away; `keep` also writes them into the
NetworkManager connection of that interface, so a reboot or a USB adapter
plugged in again comes up the same way. Both also spread the packets of an
interface with a single receive queue (a USB adapter) over the CPUs: its
interrupt on a CPU of its own, the IP and UDP work on the others, which took
such an adapter from 53 missed datagrams a second to between 1 and 5 at
320 Msps ([`tools/r8152-rtl8159`](../tools/r8152-rtl8159/README.md)). That matters: an adapter back at an
MTU of 1500 drops the stream's 8 KiB packets without a word. The controller
checks it where it runs on the receiving host, and refuses to start a stream
its interface would drop, naming this command.

Settings (serial device, addresses, ports, SSH key) default as shown in
[`install.example.json`](install.example.json); copy it to
`~/.config/w1700k/install.json` to change them, or pass the matching flags.

## First installation

```sh
make                       # if not done yet
init/install               # then power the unit on when asked
```

It stops stock U-Boot at power-on and then:

1. boots the write-free dumper from RAM, names the NAND part (see below),
   reads the unit's serial number, and copies the NAND's data as stock
   firmware addresses it (through the vendor's bad-block tables, every page
   corrected by the chip's ECC; 446 MiB) to
   `~/.local/share/w1700k/units/SERIAL/nand.bin`;
2. saves the unit's radio calibration (its MT7996 EEPROM image, from the
   `calibration` partition in the backup) as `eeprom.bin` beside the backup,
   where the host tools find it; `init/eeprom --unit SERIAL` does the same for
   an existing backup;
3. builds the unit's root filesystem with your SSH key and a host key made for
   this unit, and writes `known_hosts` for it beside the backup;
4. writes our U-Boot into the `loader` partition through stock U-Boot, reads it
   back and boots that copy;
5. from our U-Boot, erases `system` and `durable`, creates their UBI volumes,
   and writes and reads back the system FIT, the root and the durable image;
6. changes stock U-Boot's `bootcmd` to boot our U-Boot from the `loader`
   partition (the old one is kept as `bootcmd_orig`);
7. boots the installed system and checks it.

The unit is then reachable with `ssh root@192.168.1.1`. A full run takes about
five minutes. Every step is checked before the next one starts; if a run is
interrupted, run it again: the backup is kept and is never overwritten.
`--expect-serial SERIAL` stops before anything is written unless the unit is
the one you meant, and `--backup-only` stops after the backup.

The backup is the only copy of the stock firmware and of the unit's factory
data (it includes the unit's stock passwords). Keep it. Steps 4 to 6 replace
the stock operating system; `init/restore` puts it back (below).

### The NAND part

The W1700K and most W1700K2s carry a W25N04KV, on which all of this has run.
Some W1700K2s carry a W25N04LV (4 KiB pages, 256 KiB blocks, ID `ef 8b 23`).
It is supported throughout (our U-Boots, Linux, the bad-block engine, the
images, backup, restore), but has never been run on a unit, so the installer
stops on one, before the backup and with nothing written:

```
UNTESTED NAND IC DETECTED!
PLEASE SEND RAW NAND DUMP TO MAINTAINERS.
Restart with --i-dont-care-about-nand-corruption to continue at your own risk.
```

Take the raw dump first (`init/rawdump`, below) and send it in: it is what
confirms or corrects how the part is handled, its spare layout (where the
chip keeps the free bytes and the ECC parity of a written page) and its
vendor bad-block tables among them. Its bad-block reserve follows the vendor
rule read from the W1700K's stock U-Boot, which does not know the part (see
`platform/README.md`). Before writing, the installer checks that rule against
the pool the unit's own stock U-Boot prints at its boot, and stops on a
difference; vendor tables that do not validate under it are refused too. Both
stops write nothing, flag or not. `--backup-only` needs no flag, and
`init/restore` needs the same flag on such a part. Any other part is refused.

## Raw NAND dump

```sh
init/rawdump                   # to ~/.local/share/w1700k/units/SERIAL/nand-raw.bin
init/rawdump --output FILE
```

The dumper drops the vendor translation and reads every page of the chip with
the ECC off: every page's main area in physical order, then every page's OOB
(544 MiB on either part), hashed on the device and checked on the host.
`nand-raw.json` beside it records the part, its geometry, the layout, whether
the vendor tables validated, and the SHA-256. Nothing is written to the unit,
which is reset afterwards and boots as before. It takes about three minutes.
The image carries the chip's uncorrected bit errors and is not a backup;
restores use `nand.bin`. Like the backup it holds the unit's stock passwords:
send it to the maintainers only.

## Updates

```sh
make
init/flash --system        # kernel, device tree and root
init/flash --loader        # our U-Boot
init/flash --durable       # erase durable storage
init/flash --detect-only   # report where the unit is; sends one Ctrl-C
```

`init/flash` works from whatever state the unit is in (Linux, any U-Boot
prompt, or powered off, in which case it asks for a power cycle), writes only
what was asked, skips a volume that already holds the new bytes, reads every
write back, and boots the result. `--rootfs` and `--fit` update one volume of
the system each. With more than one installed unit, name it with `--unit SERIAL`.

### The root over the network alone

```sh
make
init/flash --rootfs --ssh     # no serial console: ssh to the unit, and its loader does the writing
```

The root holds the driver, the unit's programs and its start-up scripts, which
is what most updates change; it can be replaced with nothing but the Ethernet
cable to `lan3`. The kernel image (`--fit`), the loader and durable storage
still take the serial console.

What happens:

1. The new root is copied to the unit over ssh and left in its durable
   storage, with a copy of the root that runs beside it
   (`system-update stage`, in the unit's `/usr/sbin`; the tool brings its own
   copy, so an older root is updated the same way).
2. The unit reboots. Its loader finds the staged root, checks it against the
   digest left with it, writes it to the `rootfs` volume, reads that back, and
   boots it. Linux itself never writes the system flash.
3. When the unit answers again on the new root and its storage check passes,
   the tool confirms it (`system-update confirm`) and the staged files are
   removed.

If the new root does not come up, the tool says so and asks for a power cycle:
a boot that finds the update still unconfirmed puts the previous root back, and
does not try that image again. So does any later reboot of a new root that was
never confirmed; a run that was interrupted before confirming is finished by
running the same command again. `ssh root@UNIT system-update status` shows what
runs, what is staged and what the loader made of it, and `system-update cancel`
there forgets a staged root: before the reboot nothing is written at all, and
after a new root was put back it clears the note of that. The whole run takes
about a minute. (`verify-storage` on the
unit holds that nothing has written flash since the boot: the tool runs it
before confirming, and confirming, which removes the staged files, is itself
such a write, so run by hand afterwards it objects until the next boot.)

It needs a loader that applies staged updates. A unit installed before this
existed says so before anything is sent; give it the new loader once, over the
serial console: `init/flash --loader`.

## Returning to stock

```sh
init/restore                   # the backup init/install took of the connected unit
init/restore --unit SERIAL     # the same, but stop unless the unit is SERIAL
init/restore --backup FILE     # another backup file of the connected unit
init/restore --force           # past a serial or calibration mismatch (below)
```

On a W25N04LV it also needs `--i-dont-care-about-nand-corruption` (see "The
NAND part" above).

The restorer reads the unit's serial from its calibration partition; the backup
must hold the same serial, and the vendor boot stages and the calibration
partition (which holds the serial) must still match it, or nothing is written.
Those two are never written. `--force` restores the rest anyway, reporting the
mismatch: another unit's stock firmware onto this one, or a unit whose
calibration partition is damaged (it keeps its own, as it is).

From whatever state the unit is in, it stops stock U-Boot and boots the
restorer from RAM through it, as the installer boots the dumper, so a unit
whose installed loader no longer starts is restored all the same. The
restorer lifts the switch's 5 Mbit/s CPU-port limit that stock leaves behind,
receives the whole backup gzip-compressed (90 to 150 MiB for a stock unit,
seconds at about 20 MiB/s), unpacks it and checks its SHA-256 in RAM, and
checks that the vendor boot stages and calibration, which nothing here writes,
still match it.
On the device, `nandsync` then compares every block of stock's images, then of
stock's environment, with RAM, and erases, writes and reads back only the
blocks that differ, through the vendor bad-block tables with the chip's ECC on.
Everything written is compared once more, and the unit resets into its stock
firmware. Nothing is sent back to the host. It takes three to seven minutes,
most of it rewriting what changed (about ten blocks a second).

## Recovery

- The installed system does not boot: `init/flash --recovery` boots the
  recovery image (the same kernel with the root in RAM) from our U-Boot without
  writing anything; then `init/flash --system`.
- Our U-Boot does not start: power-cycle; the tools stop stock U-Boot and
  `init/install --force` reinstalls everything, keeping the backup.
- An update over the network (`init/flash --rootfs --ssh`) left the unit
  unreachable: power-cycle it; its loader puts the previous root back.
- The SSH host key of a unit installed elsewhere can be kept with
  `init/provision --unit SERIAL --import-host-key FILE` before the first
  `init/flash --rootfs`.

## Files

| Path | Contents |
| --- | --- |
| `~/.local/share/w1700k/units/SERIAL/` | `nand.bin` (the backup), `dropbear_ed25519_host_key`, `rootfs.squashfs` (the unit's root), optional `network.conf`, `nand-raw.bin` and `nand-raw.json` after `init/rawdump` |
| `~/.local/share/w1700k/runs/` | one directory per run: the raw console transcript (`rx.bin`), an event log and saved command output |

A `network.conf` in the unit directory replaces the unit's
`/etc/w1700k/network.conf` (management and IQ port addresses, MTU) on the next
install or `init/flash --rootfs`.

While a tool runs, writing to `RUN/tx.fifo` sends bytes to the console and hands
the console to you for the rest of the run.

## How it works

| Module | Role |
| --- | --- |
| `initkit/uart.py` | console session: raw transcript, stopping autoboot, running commands to a prompt |
| `initkit/tftp.py` | TFTP server (also usable by hand: `python3 init/initkit/tftp.py --help`) |
| `initkit/console.py` | checked commands and moving between stock, dumper, restorer, loader and Linux |
| `initkit/stock.py` | getting an image into RAM through stock U-Boot's TFTP |
| `initkit/backup.py` | serial number and the backup of the NAND data, from the dumper |
| `initkit/nand.py` | which NAND part the unit carries, and the stop for an untested one |
| `initkit/layout.py` | the U-Boot commands that lay out NAND, from `platform/partitions.py` |
| `initkit/steps.py` | the write steps, each read back |
| `initkit/provision.py` | the unit's root filesystem with its keys |
| `initkit/install.py`, `initkit/update.py`, `initkit/restore.py`, `initkit/rawdump.py` | the procedures |
| `initkit/remote.py` | the root's update over ssh (`init/flash --rootfs --ssh`) |

Stock U-Boot's TFTP client gives up after ten timeouts that its broken timer
spends at about a hundred per second, so images go to stock in 32 KiB pieces,
each checked by CRC-32 in RAM before stock verifies the FIT's SHA-1 hashes. Our
U-Boot fetches whole images on port 7070 and the tools check SHA-256 before and
after every write. The console session refuses NAND and environment writes
unless the run is one that writes.
