# SPDX-License-Identifier: MIT
"""The boards one set of images runs on, and how a unit is told which it is.

Gemtek's W1700K (with the W1700K2) and its XR1710G are one design: the same
AN7581 and MT7996 on the same wiring, flash layout and stock bootloader
(docs/boards/xr1710g.md has what was measured). Every image is the same for both.
The one thing that depends on the board is done when the unit starts:

  The loader reads the serial number from the unit's factory identity, in the
  `calibration` partition, which neither it nor Linux can write. The serial
  begins with the maker's name for the board (`board_id` in the loader's
  environment), and that name goes to the system in the device tree,
  `/chosen/w1700k,board`, and on the kernel command line, `w1700k.board=`.

  The W1700K's GPS receiver is powered through GPIO 43, and the loader drives
  that line low before anything else (`gps_off`), on every board, since a board
  it cannot name must be taken for one with a receiver. On the XR1710G, where
  the line is a port LED that lights when low, it then puts the LED out again.

  The W1700K's low-power radio (Bluetooth LE, Zigbee/Thread) is held in reset
  through GPIO 24 (`efr32_off`), and only on a unit the loader has named a
  W1700K: the chip does nothing unasked, so a board that cannot be named loses
  nothing by being left alone, and a line whose use is not known is not driven.

A unit whose identity cannot be read, or whose serial begins with neither name,
is `unknown` and is treated as a W1700K for the GPS receiver (and left alone for
the low-power radio). A serial does not tell a W1700K from a
W1700K2 (one of each kind has been seen, and a W1700K2's begins "W1700K2" as a
W1700K's made in the 2020s would); the kernel tells them by their parts.

This table is what the loader's environment, w1700k-gps, w1700k-efr32-line and the tests are
held to.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Board:
    key: str                # the name the loader gives it
    name: str               # as the maker labels it
    serial_prefix: str      # how its units' serial numbers begin
    # The pin-controller line that powers its GPS receiver; None on a board
    # without one, where the loader must undo what `gps_off` did to line 43.
    gps_line: 'int | None'
    # The pin-controller line that is the reset of its low-power radio (an EFR32MG21: Bluetooth
    # LE, Zigbee/Thread), which the loader holds low unless the owner's switch says on; None on
    # a board without that chip, where the line is left alone.
    efr32_reset_line: 'int | None'


W1700K = Board('w1700k', 'W1700K', 'W1700K', gps_line=43, efr32_reset_line=24)
# GPIO 43 is the green LED of a 1 GbE port here, lit when low [measured on a unit:
# stock's device tree and its kernel's GPIO table].
# No such chip on the board [photo], and what GPIO 24 does there is not known.
XR1710G = Board('xr1710g', 'XR1710G', 'XR1710G', gps_line=None, efr32_reset_line=None)
ALL = (W1700K, XR1710G)
UNKNOWN = 'unknown'


def for_serial(serial):
    """The name the loader gives a unit with this serial: a board's key, or UNKNOWN. As the
    loader compares: longer than the prefix and beginning with it."""
    for board in ALL:
        if serial.startswith(board.serial_prefix) and len(serial) > len(board.serial_prefix):
            return board.key
    return UNKNOWN
