# The Bluetooth/Zigbee radio and the GPS receiver

Beside its three Wi-Fi radios the W1700K carries two more parts that the stock firmware
barely used, each on a serial port of the SoC:

| | Part | Port on the unit | Speed | Off means |
| --- | --- | --- | --- | --- |
| Bluetooth LE, Zigbee, Thread | Silicon Labs EFR32MG21 (2.4 GHz, 10 dBm, its own printed antenna) | second UART, `/dev/ttyS1` | 115200 (factory firmware); the port reaches 230400 and 460800 | held in reset |
| GPS | Airoha AG3352 (GPS, GLONASS, Galileo, BeiDou) | high-speed UART, `/dev/ttyS2` | 921600 | power switched off |

Nothing on the unit uses either. What this project gives you is each one's serial port on
your host, carried over ssh, where it looks like a serial device: point a Bluetooth host
stack or a flasher at the radio, gpsd or pygpsclient at the receiver.

Both are the W1700K's (and the W1700K2's). The XR1710G has neither, and the commands say so
there.

## Off unless you turn it on

Both are **off at every start-up** unless the unit's owner has said otherwise, and each
one's serial port **does not exist** on a unit where it is off:

- **The receiver** would otherwise say where the unit is, once a second, from power-on.
  The loader switches its power off before anything else runs.
- **The radio** runs whatever firmware it holds from power-on. The loader holds it in reset.
- **The owner's switch** for each is a small file in the unit's durable storage, written by
  `on --keep` and `off --keep`. The loader reads it at every boot. Where it says on, the
  loader lets that part run and enables its serial port in the device tree it hands to
  Linux; where it does not, the port stays disabled and no program can open it.

So the first use of either takes two steps: keep it on, then restart the unit. The restart
is needed because Linux creates its serial devices from the tree it is handed at boot. `on`
without `--keep` runs the part until the next start, with nobody able to reach it.

## From the host

The command is `host/bin/w1700k` in a checkout (it is not installed on your `PATH`). It
finds the unit from `~/.config/w1700k/config.json`, or from the file `$W1700K_CONFIG` names
([host/w1700k](../host/w1700k/README.md#configuration)). None of these commands touches the
receiver of ours or starts its controller.

### The GPS receiver

```sh
host/bin/w1700k gps on --keep        # on now, and at every start-up from now on
# restart the unit (power, or `reboot` over ssh), wait for it to come back
host/bin/w1700k gps status           # {"gps": "on", "start": "on", "port": "on"}
host/bin/w1700k gps check            # does it talk, has it a fix
host/bin/w1700k gps pty --name ~/w1700k-gps     # its port as a device, until stopped
host/bin/w1700k gps off --keep       # off again, now and at every start-up
```

- **`status`** has three words. `gps`: whether it has power now. `start`: what start-up
  does. `port`: whether its serial port is there, which is so only at a boot where the
  switch already said on.
- **`check`** listens for three seconds (`--seconds N`) and prints the kinds of sentence it
  heard, how many lines were not sentences, whether there is a fix and how many satellites
  it uses:

  ```
  {"sentences": {"GGA": 3, "GLL": 3, "GSA": 12, "GSV": 33, "RMC": 3, "VTG": 3, "ZDA": 3, ...},
   "bad": 0, "fix": "3d", "satellites": 22}
  ```

  It never prints the position and does not look at it. It writes nothing to the receiver.
  `"fix": "none"` with sentences arriving and `"bad": 0` is a receiver that works and is
  still searching: it takes about a minute after being switched on or restarted, longer
  with no view of the sky.
- **`pty`** prints a device path and stays in the foreground until interrupted. That device
  is the receiver's port: NMEA 0183 comes out of it, a position a second, and anything
  written to it goes to the receiver. Without `--name` the path is `/dev/pts/N`, a
  different one each run. With `--name PATH` it also makes a link at `PATH` for as long as
  it runs, so a program's settings can name it:

  ```sh
  host/bin/w1700k gps pty --name ~/w1700k-gps &
  pygpsclient                                  # with ~/w1700k-gps as its user-defined port, 921600
  gpsd -N -n ~/w1700k-gps                      # then cgps, gpsmon, anything that asks gpsd
  ```

  The device is a pseudo-terminal, so the speed a program sets on it does not matter; the
  real port runs at 921600. `--baud 115200|230400|460800` is for a receiver you have told
  to send at another speed.

What the receiver sends, as seen on a unit: GGA, GLL, GSA, GSV (four constellations), RMC,
VTG, ZDA, GST, and Airoha's own EPE and `PAIR` lines.

### The Bluetooth/Zigbee radio

```sh
host/bin/w1700k efr32 on --keep      # let it run, now and at every start-up
# restart the unit
host/bin/w1700k efr32 status         # {"efr32": "on", "start": "on", "port": "on"}
host/bin/w1700k efr32 identify       # ask it what firmware it runs
host/bin/w1700k efr32 pty --name ~/w1700k-efr32     # its port as a device, until stopped
host/bin/w1700k efr32 off --keep     # held in reset again, now and at every start-up
```

- **`identify`** sends the two commands of Silicon Labs' BGAPI that every Bluetooth
  co-processor image answers and that make it do nothing else. The factory firmware is such
  an image:

  ```
  {"firmware": "bgapi", "stack": "6.2.0", "build": 212, ...}
  ```

  Other firmware (a Zigbee or Thread co-processor) does not answer it, and that is said.
- **`pty`** is the radio's port as a device, at 115200 baud unless `--baud 230400` or
  `--baud 460800` says otherwise. The factory firmware takes BGAPI commands there, so a
  BGAPI host library can scan, advertise and connect through it.
- **`start` can also read `unheld`**: the unit's loader is older than this feature and does
  not hold the radio, which then runs from power-on until `off` is said.

**What goes through the radio's port is your doing.** A host that tells the radio to
advertise, scan actively or connect makes the unit transmit at 2.4 GHz, up to 10 dBm,
beside its own 2.4 GHz receive antennas, and the 2.4 GHz captures will show it. Nothing in
this repository does that, changes the radio's firmware, or drives its bootloader line.

## On the unit

The host commands run these over ssh; they can be run there directly.

| | |
| --- | --- |
| `w1700k-gps status \| on [--keep] \| off [--keep]` | the receiver's power and its switch |
| `w1700k-gps link [--baud N]` | the receiver's port on standard input and output |
| `w1700k-efr32 status \| on [--keep] \| off [--keep]` | the radio's reset and its switch |
| `w1700k-efr32 link [--baud N]` | the radio's port on standard input and output |

`link` is the whole mechanism: `ssh root@unit w1700k-gps link` is the receiver's port on the
ssh session's input and output, byte for byte, and the host's `pty` joins that to a
pseudo-terminal. Any other way of carrying a byte stream over ssh works as well.

## How it is put together

```
 host                                    unit
 ----                                    ----
 gpsd, pygpsclient, a BLE host ...
        |  opens
 ~/w1700k-gps -> /dev/pts/N
        |  w1700k gps pty
        +---- ssh ---->  w1700k-gps link        refuses if the receiver is off or the port is not there
                              |  runs
                         w1700k-port-link       joins the port to its input and output
                              |
                         /dev/ttyS2             there only at a boot where the switch said on
                              |
                         the receiver           powered by line 43 of the pin controller
```

The radio is the same with `efr32` for `gps`, `/dev/ttyS1`, and line 24 (its reset).

- **Loader** (`platform/loader/an7581_w1700k_sdr_env`): `gps_off` and `efr32_off` first, at
  every boot; `gps_pass` and `efr32_pass` read the switches (`gps/switch`, `efr32/switch` in
  durable storage) and tell Linux what they did (`w1700k.gps=on|off`,
  `w1700k.efr32=on|off` on the kernel command line); `gps_give` and `efr32_give` enable the
  ports in the device tree.
- **Device tree** (`platform/linux/dts`): both ports have their pins and fixed names
  (`serial1`, `serial2`) and are disabled as built. The build refuses a tree that enables
  either.
- **Unit programs**: `w1700k-gps-line` and `w1700k-efr32-line` drive the two lines;
  `w1700k-gps` and `w1700k-efr32` are the owner's commands; `w1700k-port-link` is the link.
- **Host** (`host/w1700k`): `portlink.py` (the link and the pseudo-terminal), `gps.py`,
  `efr32.py`.

The reasons behind each choice are in
[platform/README.md](../platform/README.md#the-gps-receiver).

## The speed of the two ports

The kernel's driver for these UARTs, as OpenWrt carries it, gave neither port the speed it
was asked for. The radio's ran at half of 230400 and a quarter of 115200; the receiver's ran
at twice what was asked. Our kernel patch 0015
(`platform/linux/patches/0015-serial-8250-airoha-the-speed-asked-for.patch`) corrects both,
the same way the driver proposed for mainline Linux does.

Measured on a W1700K with the patch, by timing transmissions of two lengths:

| Port | Asked | On the wire |
| --- | --- | --- |
| radio (`ttyS1`) | 115200 | 115,344 |
| radio | 230400 | 229,825 |
| radio | 460800 | 460,800 |
| receiver (`ttyS2`) | 115200 | 115,200 |
| receiver | 460800 | 458,507 |
| receiver | 921600 | 912,475 |

The timing resolves about 1 %, so each is the speed asked. The radio's factory firmware
answers when 115200 is asked for and at no other speed; the receiver is read at 921600 and
at no other.

## When it does not work

| What you see | What it means |
| --- | --- |
| `w1700k: command not found` | The command is `host/bin/w1700k` in the checkout. |
| `the receiver's port is not there: the loader enables it where the receiver is kept on` | The switch was not on when the unit last started. `gps on --keep`, then restart the unit. The same for the radio. |
| `the receiver is switched off: nothing would be heard` | `gps on` (and `--keep` for it to last). |
| `the radio is held in reset: nothing would answer` | `efr32 on`. |
| `"port": "off"` right after `on --keep` | Expected: the port comes with the next start. |
| `"fix": "none", "satellites": 0` | The receiver is talking and still searching. Wait a minute; give the unit a view of the sky. |
| `the receiver said nothing in 3 s` | It was switched on a moment ago and has not started sending yet. |
| `what the receiver sent is not NMEA` | The wrong speed for what the receiver is set to. |
| `the radio did not answer` or `is not BGAPI` | Other firmware in the radio, or another speed. |
| `this board (xr1710g) has no GPS receiver` | That board has neither part. |
| `this unit's loader does not read the switch yet` | The unit's loader is older than this. Update it (`init/flash --loader --ssh`). |

## Privacy and trust

- **Where a unit is stays with its owner.** Nothing on the unit reads, stores or uses what
  the receiver says, and it leaves the unit only through the owner's own ssh session.
  Nothing in the host package stores it either.
- **GPS is not a trust input.** Nothing a unit accepts (an update, a key, a login) is judged
  by GPS time or place, which anyone nearby can transmit.

## What has not been done

- gpsd has not been run against the pseudo-terminal. PyGPSClient has, with the name given
  to `--name` as its user-defined port at 921600: it showed a 3D fix and every kind of
  sentence above. A program reading it through pyserial got every line as a sentence whose
  checksum holds.
- No Bluetooth host stack has been run through the radio's port; only `identify`.
- 230400 and 460800 on the radio's port were measured by timing, not against a device that
  talks at them: the factory firmware talks only 115200.
- All of this was measured on one unit (hardware 2.1).
- Nothing runs on the unit itself: no GPS reader there, and no Bluetooth, Zigbee or Thread
  stack ([the roadmap](roadmap.md)).
