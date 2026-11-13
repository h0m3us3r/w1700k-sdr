# SPDX-License-Identifier: GPL-2.0-only
# Linux 7.3's r8152 (the first releases with the RTL8159, USB 0bda:815a) built for an older
# running kernel, whose own r8152 leaves the adapter to cdc_ncm (MTU 1504, no jumbo frames).
#
#   make                 # r8152.ko for the running kernel (needs its linux-headers); not as root
#   sudo make install    # into /lib/modules/KVER/updates/, which takes precedence over the kernel's own
#   sudo make uninstall
#
# KVER=... builds for another installed kernel (repeat after a kernel update).
#
# r8152.c is drivers/net/usb/r8152.c of Linux v7.3, unmodified
# (sha256 9d766f203c7fdabe6449e8dd2b9e5dd4fccfa20a62aa5dbf00d64c0c475c5a9f). It asks for
# rtl_nic/rtl8159-1.fw (the PHY patch), which linux-firmware does not carry; without it the
# driver still binds, and the link is reported to come up at 5 Gbit/s instead of 10.

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build
DEST := /lib/modules/$(KVER)/updates/r8152.ko

.PHONY: all install uninstall clean

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

install:
	@test -f r8152.ko || { echo "build first: make (not as root)" >&2; exit 1; }
	install -D -m 0644 r8152.ko $(DEST)
	depmod -a $(KVER)

uninstall:
	rm -f $(DEST)
	depmod -a $(KVER)

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
