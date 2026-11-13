# Platform: what runs on the W1700K's SoC

The unit boots in three stages:

```
stock U-Boot (vendor, first stage, never replaced)
  -> our U-Boot, the loader     FIT in the `loader` partition, read by stock's bootcmd
  -> our Linux                  FIT in UBI volume `fit`, root SquashFS in volume `rootfs`
```

Linux runs from a read-only SquashFS under a RAM overlay, loads the
`w1700k_sdr` driver at boot, brings up the management port (`lan3`,
192.168.1.1/24) and the 10 GbE IQ port (`eth1`, 10.77.0.1/24, MTU 9216), and
starts Dropbear for key-only root logins. Nothing at startup writes NAND.

## Building

`make` in the repository root builds everything in order; the pieces are:

| Step | Script | Output (under `platform/build/`) |
| --- | --- | --- |
| toolchain | `rootfs/build.py toolchain` | Buildroot cross toolchain, packages and host tools in `buildroot/` |
| linux | `linux/build.py` | `linux/arch/arm64/boot/Image`, `linux/sdr/an7581-w1700k-sdr.dtb` |
| sdr | `make -C linux module reader` | `kernel/w1700k_sdr/build/`: the module and the four clients |
| rootfs | `rootfs/build.py image` | `buildroot/images/rootfs.squashfs` with the driver and clients |
| loader | `loader/build.py` | `images/w1700k-loader.itb` (and `mkimage/`) |
| images | `images.py` | `images/w1700k-system.itb`, `w1700k-recovery.itb`, `rootfs.squashfs`, `durable-w25n04kv.ubifs`, `durable-w25n04lv.ubifs` |
| dumper | `dumper/build.py` | `images/w1700k-dumper.itb` |
| restorer | `restorer/build.py` | `images/w1700k-restorer.itb` |

A first build downloads about 500 MB of sources and builds a GCC toolchain;
on a 20-core host the whole `make` takes about ten minutes plus the downloads.
Later builds redo only what changed (a no-change platform build takes seconds). Each script logs to
`platform/build/logs/` and prints the end of the log when a command fails.
`JOBS=N` limits parallel jobs. `make clean` keeps downloads and prepared
sources; `make distclean` removes `platform/build/` entirely.

Host requirements: a C compiler, make, git, patch, Python 3.10+, perl, rsync,
bc, cpio, unzip and the usual Buildroot prerequisites. Bison and flex are built
by Buildroot. If the host's `install` is not GNU coreutils (some distributions
ship uutils), `gnuinstall` or `ginstall` must exist.

## Sources

[`sources.json`](sources.json) pins every upstream input: Buildroot by tag and
commit, Linux and U-Boot by tarball SHA-256, the OpenWrt tree (kernel patches,
the Airoha device tree and the RTL8261N PHY firmware generator) by commit, the
PHY firmware's own generator files and output by SHA-256, and the RTL8261CE's
firmware file (the W1700K2's PHY) from linux-firmware by commit and SHA-256.
[`fetch.py`](fetch.py) downloads and checks them; `W1700K_OPENWRT_GIT` points it
at another OpenWrt repository (for example a local mirror) while still
requiring the pinned commit.

## Layout

| Directory | Contents |
| --- | --- |
| `rootfs/` | Buildroot configuration and the overlay: init scripts, network and storage configuration, `verify-storage`, the `durable-*` helpers, `system-update` |
| `linux/` | kernel configuration, the board device tree, our kernel patches, the Linux BMT binding, `board_check.py` |
| `loader/` | our U-Boot: defconfig, default environment, device tree, patches, the U-Boot BMT binding |
| `dumper/` | the write-free dumper: defconfig, environment, device tree, patches; it links the loader's BMT binding read side only, with `bmtdetach` for raw dumps |
| `restorer/` | the restorer that writes a backup back: defconfig, environment, patches (`nandsync`); the dumper's device tree |
| `patches/` | U-Boot patches the loader, the dumper and the restorer all carry (among them the W25N04LV and the part named at probe) |
| `bmt/` | the vendor bad-block translation and replacement engine, shared by our U-Boots and Linux, and its C test harnesses |
| `bootstrap/` | the initramfs `init` that mounts the root |
| `partitions.py` | the NAND parts and layout, generated into both device trees and the BMT policy header |

## NAND

512 MiB SPI-NAND: a W25N04KV (128 KiB blocks, 2 KiB pages) or, on some
W1700K2s, a W25N04LV (256 KiB blocks, 4 KiB pages). Offsets are vendor-logical:
every stage translates them through the vendor's bad-block tables (BBT shift,
then BMT substitution) exactly as stock U-Boot does, so all stages agree on
where a partition is. The partitions are the same bytes on both parts, every
boundary on a 256 KiB block; the UBI volumes are sized per part.

The vendor reserve is 250 blocks on the W25N04KV, fixed by the 250-entry tables.
On the W25N04LV it is what stock's boot computes from the factory bad-block
markers: 163 good blocks counted down from the last, plus the factory-bad
blocks among them (U-Boot `0x81e1b540`), and the BMT checksum covers that many
entries. That rule is read from the W1700K's stock U-Boot, which does not know
the W25N04LV; a unit with that part ships a newer one, and nothing here has
read its tables yet. Tables that do not validate under the rule are refused,
never rewritten.

| Partition | Offset | Size | Contents | Linux |
| --- | --- | --- | --- | --- |
| `bootloader` | 0x0000000 | 2 MiB | vendor BL2/BL31 and stock U-Boot | read-only |
| `uenv` | 0x0200000 | 2 MiB | stock U-Boot's saved environment | read-only |
| `calibration` | 0x0400000 | 2 MiB | identity and radio calibration | read-only |
| `loader` | 0x0600000 | 2 MiB | our U-Boot FIT | read-only |
| `system` | 0x0800000 | 48 MiB | UBI: `ubootenv`, `ubootenv2`, `fit`, `rootfs` | attached read-only |
| `durable` | 0x3800000 | 32 MiB | UBI: `durable`, a UBIFS for persistent files, and for a root update on its way in (below) | detached; `durable-open` |
| `scratch` | 0x5800000 | 358 MiB | unused | read-only |
| `reserved-bmt` | 0x1be00000 | 66 MiB | vendor bad-block reserve pool | read-only |

Writes program erased pages directly, in ascending order, and read each page
back: a block's state is known from an erase in the same boot or, the first
time it is written in a boot, from reading its pages down to the highest one in
use (UBI keeps its free blocks erased across boots). One program per page, one
erase per block reused. The BMT layer replaces a block only when a program or
erase fails (journalled in the block after the BBT, so an interrupted
replacement recovers on the next boot), and only ever writes inside
`system`..`scratch` (the restorer: `uenv`..`scratch`, except `calibration`). It
writes only while the BBT is the reserve's first good block, as the vendor
places it: otherwise the pool it computed is not the one stock computes. A set
factory marker is read twice in the pool scan (the byte has no ECC). Before
anything is written, the installer and the restorer check that the pool stock
U-Boot printed at its boot in the same run (`bmt pool size: N`) is the one our
U-Boot computed. Linux waits for the chip by polling its status with `udelay()`
(patch 0012): the kernel has no high-resolution timers, so a sleeping poll
would take a 4 ms tick per page.

## The 10 GbE ports' PHYs

The W1700K2's RTL8261CE takes a firmware patch (`rtl_nic/rtl8261c.bin`, built
into the kernel) that the PHY driver writes into it over MDIO: 4714 register
operations. Two of our kernel patches are about that.

- **0013, the patch arrives whole, and every time.** The driver as backported
  wrote an entry of the file only when the register did not already read that
  value. The patch's code is a run of writes to one register (`0xa438`), each
  taking the next address, so every word left out put the rest in the wrong
  place, and the PHY came out of its first start after boot with a damaged
  patch. The driver also loaded the file once per boot, while the PHY's reset
  line drops it whenever the port is taken down, so a port brought up a second
  time ran without any. Now every entry is written, on every start of the port.
- **0014, the bus is not slow.** The PHYs sit on the switch's MDIO bus, whose
  accessors slept between polls for the end of a transaction: a 4 ms tick each
  without high-resolution timers, 38 s for the patch, during `w1700k-network`
  at boot and so before the SSH server started. They poll with `udelay()` now,
  and the port starts in under a second.

Measured on a W1700K2 whose IQ port is cabled to an RTL8159 USB adapter (which
does not train at 10GBASE-T here: both ends try three times, then agree on
5 Gbit/s):

| | before | with 0013 and 0014 |
| --- | --- | --- |
| SSH answers after a reboot | 50 to 80 s into the boot | 10.5 s |
| IQ port linked after a reboot | 92 to 124 s after Linux resets the PHY, at 5 Gbit/s or, with 5 and 2.5 Gbit/s failing three times each as well, at 1 Gbit/s | 18 s, at 5 Gbit/s (5 boots of 5) |
| IQ port linked after `ip link set eth1 down; up` | 20 s, the PHY unpatched | 18 s |

10GBASE-T between two patched PHYs was tried with a cable between the unit's
own two 10 GbE ports (`eth1` to `eth2`): both linked at 10 Gbit/s on the first
attempt, 5 to 6 s after the second port started. A 160 Msps raw stream
(5.2 Gbit/s) sent to `eth2`'s address crossed it for 30 s, 2.35 million frames,
with no CRC or other receive error counted. That loop cannot show the whole
9.75 Gbit/s of a 320 Msps stream: every frame sent comes back in, the CPU that
handles the QDMA takes about 86,000 of them a second, and the export stops
after two seconds as it falls behind at that same rate (5.4 Gbit/s; one 200 ms
sample reached 9.5). That takes a receiver that is not the unit itself.

A register reader for the unit needs no driver support: `SIOCGMIIREG` on
`lan3`, whose own PHY is on the same bus, reaches any address on it, clause 45
included (PHY 8 is `eth1`'s, PHY 5 `eth2`'s; extension register `0x801e`, read
through `0xa436`/`0xa438` in MMD 31, is `0x2215` with the patch and `0x0015`
without).

### Updating the root without a console

Linux cannot write `system`, and that stays so. To replace the root over the
network, `system-update stage` (in the root's `/usr/sbin`, driven by
`init/flash --rootfs --ssh`) leaves three files in `durable`, under `update/`:
`rootfs.new`, `rootfs.old` (the root that runs, to go back to) and, last and
by a rename, `request`, two lines naming both by SHA-256.

The loader's boot command begins with `run update_apply`
(`loader/an7581_w1700k_sdr_env`). With a valid request in `durable` it:

- writes `rootfs.new` to the `rootfs` volume once its digest matches, reads the
  volume back, and saves `update_tried` in its environment;
- on a later boot that finds the request still there and `update_tried` set,
  takes the update for one that was never confirmed: writes `rootfs.old` back
  and saves `update_failed` instead, after which that image is left alone;
- puts `rootfs.old` back at once if the write of the new root does not read
  back;
- once the request is gone (`system-update confirm` or `cancel` removes the
  files), erases its saved environment again.

An interrupted write is repeated by the next boot, since the note is saved only
after the read-back. The loader's environment is otherwise never saved, and
with `CONFIG_ENV_WRITEABLE_LIST` (loader patch 0008) the only variables a saved
copy can set are those two: the boot and update scripts are always the ones the
loader was built with. A request counts only by its content, not by the file
being there: a read of a missing file is not relied on to fail (this U-Boot's
UBIFS reader took the end of a directory for a match, loader patch 0009).
`tests/test_loader_update.py` runs the scripts as they are written, in a POSIX
shell with U-Boot's commands modelled.

The loader's FIT description carries `staged-update 1`; `system-update` reads
it from the `loader` partition and refuses to stage for a loader without it.

## Checks in the build

The build stops when an output is wrong rather than leaving it for the unit to
find: requested kernel options that Kconfig dropped, a device tree whose
devices, 10 GbE wiring, reserved memory or partitions differ from the design,
OpenWrt's `mtk_bmt` linked into a kernel or loader (it rewrites vendor tables it
fails to parse), any NAND write path or the BMT write engine linked into the
dumper, `bmtdetach` (untranslated access) in any stage that can write, a
restorer without its write path, a U-Boot runtime that would overlap its
staging area, or an image that does not fit its partition or volume. `make check` runs the unit tests, including the BMT layer
compiled for the host, every harness once per NAND part.
