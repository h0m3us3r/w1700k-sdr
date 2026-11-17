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

The second line is not needed with `--stock-transport serial` (below).

Stock U-Boot fetches files only from UDP port 69. `tftp-forward` redirects
exactly that traffic (from 192.168.1.1 to 192.168.1.10 on IFACE) to the
installer's unprivileged port, so the tools run as a normal user. Without it,
the tools must run as root to listen on port 69 themselves; root then also owns
the files they write, under root's home unless `--data-dir` and
`--authorized-key` say otherwise.

The redirect is one rule and a systemd unit that loads it at every boot:

- **Checked first.** `enable` changes nothing until its checks pass: the
  arguments, the interface, that the tool it will use accepts this very rule
  on this host's kernel, and that systemd is there to load it again.
- **nftables, or iptables.** The rule is an nftables rule in a table of its
  own. Where nftables cannot load it, `enable` says why and uses iptables: the
  same match in a chain of its own. `--backend nft` or `--backend iptables`
  names one, and then the other is not tried.
- **`--once`** loads the rule and installs nothing; it is gone at the next
  reboot. A host without systemd is offered only this.
- **`disable`** unloads the rule and removes the unit and the rule file,
  whichever way they were installed. With iptables' nftables variant an empty
  `nat` table of iptables' own may remain, as after any iptables rule.
- **`status`** says what is installed to anyone and lists the loaded rule to
  root. Without root it used to say the redirect was not loaded when it was.
- **A firewall of the host's own** (ufw, firewalld) is not changed. `enable`
  points one out, because it may drop what the redirect sends on to port 7071.

The fallback and the status are owed to vampjaz's report in
[issue 10](https://github.com/h0m3us3r/w1700k-sdr/issues/10): a host whose
nftables was installed but did not work, and a status that misled.

### Without port 69: the serial line

Stock U-Boot only ever takes two small images from the host, the dumper and our
loader; everything after them is fetched by our own U-Boot from an unprivileged
port. `--stock-transport serial` on any of the console tools (or
`"stock_transport": "serial"` in `install.json`) sends those two over the console
itself, with stock's `loadb` (Kermit). The tools then listen on UDP 69 nowhere,
so there is no redirect to set up and no need for root.

| | TFTP (the default) | Serial |
| --- | --- | --- |
| The host needs | the redirect above, or root | nothing more than the console |
| Time for the dumper (179 KiB), measured | 5 s | 21 s |
| Checked in RAM before use | `crc32`, then stock's `iminfo` | the same |

TFTP stays the default because it is the path every install so far has taken and
an Ethernet cable is the better link. Serial is for a host where the redirect
cannot be had or is not wanted. Kermit's own check is six bits a packet, so over
serial it is the `crc32` in RAM that vouches for the image; a load that fails it
stops the run before anything is written.

Measured on a W1700K2 (stock U-Boot "AXON 2.3"): both images loaded, verified
and booted, with no packet sent twice. A stock loader without `loadb`, or a line
that carries nothing, stops the run with a pointer back to TFTP. The command is
named on [OpenWrt's page for this unit](https://openwrt.org/toh/gemtek/mxf-w1700k),
which is where the idea came from.

For the 10 GbE IQ link (unit `eth1`, 10.77.0.1), give the host side its
address and jumbo frames: `sudo init/host/link keep IFACE10G 10.77.0.10/24 9216`.
`up` sets them until the interface goes away; `keep` also has NetworkManager
hold them, so a reboot or a USB adapter plugged in again comes up the same
way. It makes a connection of its own for the interface, `w1700k-IFACE`, with
the address set by hand, and has NetworkManager prefer it to the one it made
for the adapter itself: that one asks for an address by DHCP, which no unit
answers, and when the request times out NetworkManager takes every address
off the interface. (A connection of yours that already manages the interface
with a static address is used as it is.) `init/host/link down` removes what
`keep` made. Both also spread the packets of an
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

The backup is the only complete copy of what the unit held, its factory data
included (and the unit's stock passwords). Keep it. Steps 4 to 6 replace the
stock operating system in the first of stock's two firmware slots; the second
slot, a whole copy of the stock firmware, and stock's own data behind it are
not written. `init/restore` puts everything back (below).

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

### Over the network alone

```sh
make
init/flash --ssh              # the loader and the root from this build, with one reboot
init/flash --bundle FILE      # the same from an update archive (w1700k-update.tar)
init/flash --rootfs --ssh     # the root alone
init/flash --loader --ssh     # the loader alone
```

No serial console: ssh to the unit, and its loader does the writing. The root
holds the driver, the unit's programs and its start-up scripts, which is what
most updates change; both it and the loader can be replaced with nothing but
the Ethernet cable to `lan3`. The kernel image (`--fit`) and durable storage
still take the serial console.

`make` writes the archive beside the other images
(`platform/build/images/w1700k-update.tar`): the second loader, the root as
built, and a manifest of their SHA-256 digests and of the flash layout they
are for. `--bundle` checks all of that, gives the root this unit's keys as an
update from the build does, and refuses an archive made for another layout.

#### The root

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

#### The loader

Stock U-Boot reads 256 KiB at one fixed place and boots it: the `loader`
partition. Whatever is there has to work, and nothing below it is ours, so
that copy is written over the serial console only (`init/flash --loader`,
through stock U-Boot) and by nothing we run on the unit. A loader that comes
over ssh goes into the 768 KiB beside it, `loader-b`:

1. The second loader (`w1700k-loader-b.itb`, the same build as the first) is
   left in durable storage (`system-update stage-loader`).
2. At the reboot the first loader checks it against the digest left with it
   and that it is a second loader, writes it to `loader-b`, reads it back,
   notes that it is being tried, and starts it. From then on the first loader
   starts the second at every boot, after checking its hashes; the second
   boots the system (and writes a staged root, when there is one).
3. The second loader notes that it ran, and the tool confirms the update when
   the system is up under it.

A boot that finds the update still unconfirmed removes the second loader and
does not try that image again: a loader that hangs, or under which the system
does not come up, costs a power cycle. A `loader-b` that fails its hashes at
any later boot costs nothing: the first loader boots the system itself, in
that same boot, and the next update over ssh writes `loader-b` afresh.

All of that has been made to happen on a unit (a W1700K2), each time through
`init/flash --loader --ssh` with the console only watched:

| What was done to it | What the unit did |
| --- | --- |
| A second loader that hangs for good: it prints nothing, and the power is cut and restored by hand three minutes later | Next boot: "the new loader was not confirmed; removing it", and the system comes up on the first loader. The tool, which had asked for the power cycle, reports that the loader was written but not kept. |
| The same, the unit reset 15 s into the hang instead | The same. |
| A second loader that runs, notes that it ran, and never boots the system | The same. |
| A second loader that is no image U-Boot can start (its hashes verify) | The first loader faults at the hand-over and resets itself; the next boot removes it as above. |
| Installed and confirmed, then two bytes changed in flash | "what is in loader-b is not a whole second loader; not started"; the system boots on the first loader in that boot. |
| ... then its second block erased (a torn write) | The same. |
| ... then its first block erased | Not taken for a loader at all; the system boots. |
| ... then a byte changed in the image tree's own structure | As for two bytes changed. |

After each of the four kinds of damage the same command put a good second
loader back and confirmed it. Two things to know from these runs. A loader that
hangs for good needs someone to cut the power: nothing on the unit resets it.
And `system-update status` gives a damaged `loader-b` a digest like any other;
it is the next `init/flash --loader --ssh` that sees it is not the loader it
should be and replaces it.

A countdown of five seconds on the console belongs to the first loader, and one
of a second to the second; `init/flash --loader` over the console also clears
`loader-b`, so that what was just written is the loader.

Both updates need a loader that knows them. A unit installed before they
existed says so before anything is sent; give it the new loader once, over the
serial console: `init/flash --loader`.

### A unit installed from an earlier version

The flash layout changed twice in November 2026. Until then our `durable`
storage reached 18 MiB into stock's second firmware slot, the copy of the
stock firmware that stock keeps beside the one it boots; everything of ours
now lies in the first slot. And the megabyte for loaders was divided in two
for the second loader, which moved `system` down by one (`platform/README.md`
has the table). Every update compares a unit's partitions with the build's and
stops before writing anything to one laid out differently: over the network
from what its Linux lists, over the serial console from what its installed
loader lists (`mtd list`), which for `init/flash --loader` is read before that
loader is replaced. Such a unit is moved in two steps, both over the serial
console:

```sh
init/install --force       # the same backup is kept; `system` and `durable` are made again, smaller
init/restore --stock-slot  # stock's second slot and data, from the backup; our system stays
```

The first takes a minute and a half and empties `durable`. The second is
needed only for a unit installed before the second slot was left alone: it
sends the backup to the unit, compares stock's part of the chip with it block
by block, and rewrites what differs: on the unit this was done with, the 144
blocks the old `durable` had covered and nothing else. It then boots the
installed system again.

## The paperclip key

A unit has one slot for a login key that only a hand on the unit can change:
its reset button, pressed with a paperclip. It needs no serial console and no
key on the unit beforehand, so it is how a unit gets its first key when it was
installed without yours, and how you get back in when a key is lost.

```sh
init/key                      # your public key (~/.ssh/id_ed25519.pub, id_ecdsa.pub, id_rsa.pub)
init/key FILE.pub
```

Run it, then hold the button. What a press means is decided when you let go,
by how long you held it, and the LED says which stretch you are in
([docs/led.md](../docs/led.md#the-reset-button)):

| Held | The LED while held | Letting go |
| --- | --- | --- |
| under 5 s | green rising or red sinking (below) | nothing: what had filled runs out |
| 5 s | a flash, then the same colour pulsing | switches the slot's key off, or on again |
| 10 s | a flash, then blue pulsing | opens the slot for a new key: three minutes, blue breathing |
| 15 s | a flash, then red blinking hard | erases the slot's key |

The first five seconds already say which way the key would be switched, in
colour and in direction: green rising out of the dark when it is off and would
be switched on, red sinking from full when it is on and would be switched off.
With an empty slot there is nothing to switch, so nothing happens before ten
seconds: white rises all that way, without the flash at five.

While the slot is open the unit takes one line of text, a public key, on TCP
port 7722 of its management address (`lan3`). It checks that the line is a
whole public key of a type its ssh server takes, keeps it switched on, and
answers; `init/key` then logs in with it. A line that is not a key is refused
and the wait goes on; a press ends the wait. A new key replaces the one the
slot held.

The unit's answer carries its ssh host key. `init/key` tries the login against
exactly that key and prints the line for `known_hosts` (`--known-hosts FILE`
writes it there): it came over a cable to a unit you are holding a paperclip
to, which is as good an introduction as a host key gets.

- **The key stays** across restarts and updates: it is not part of the root.
  It is a file in durable storage that the loader reads at every boot and
  hands to the system, so a normal boot still writes nothing to flash and
  Linux opens durable storage only when the button changes the slot.
- **Switched off, it is kept but does not log in**, and stays off across
  restarts until the button switches it on again. A unit whose root has no key
  built in is then closed to the network altogether.
- **It needs a loader that knows of the slot.** `init/key` says so when the
  unit's does not: the key then works until the unit restarts. `init/flash
  --ssh` brings the loader up to date.
- **It is the button's, not the network's.** Nobody without a hand on the unit
  can change the slot, and whoever has one could open the case and use the
  serial console anyway. The slot holds what was last put in it: program your
  own key into a unit that comes from someone else.
- The key that `init/install` builds into the unit's root is separate and
  stays as it is; both log in.

### A unit with no key built in

By default a unit's root carries the installer's public key. `--no-login-key`
leaves it out, for a unit that is to take its key from its reset button
alone, a unit made for someone else, say:

```sh
init/install --no-login-key           # over the console: no key file is needed
init/flash --rootfs --no-login-key    # the same for an installed unit, over the console
init/flash --ssh --no-login-key       # or over ssh, to a unit that has a paperclip key
```

Such a unit answers ssh from nobody until `init/key` and a paperclip give it
a key. Over ssh the tools therefore send a root without a key only to a unit
whose paperclip key is switched on and kept by its loader; log in with that
key (`--authorized-key` names the key the tools log in with, as always), or
the update cannot be confirmed and the next boot undoes it.

The option is asked for each time. A later root made without it carries a key
again; the tools say so when the last root made for that unit had none (the
unit directory's `login-key` notes which).

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

Stock U-Boot can lose characters of a line that reaches it in one burst (seen
on one unit: four characters of a 26-character line gone from its echo). A lost
digit of an address makes another command, so the console session sends a
character per millisecond, and at a U-Boot prompt it types a command, compares
the echo with what it sent, and presses Enter only when they agree. A line that
came back damaged is discarded with Ctrl-C and typed again; after three damaged
tries the run stops with nothing entered. The echo that confirms the console
after stopping the countdown is repeated the same way.
