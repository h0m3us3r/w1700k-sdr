# SPDX-License-Identifier: MIT
# The kernel itself is built by build.py; this file builds what is compiled
# against it: the w1700k_sdr module and the static unit-side clients.
ROOT := $(abspath ../..)
OUT := $(ROOT)/platform/build/linux
CROSS_COMPILE ?= $(ROOT)/platform/build/buildroot/host/bin/aarch64-buildroot-linux-musl-
export PATH := $(ROOT)/platform/build/buildroot/host/bin:$(PATH)
.PHONY: kernel module reader
kernel:
	python3 build.py
# The SDR driver and its unit-side clients. KDIR is a configured, built kernel
# tree (default: this directory's build output).
KDIR ?= $(OUT)
SDR := $(ROOT)/kernel/w1700k_sdr
# Built the same way, but not clients of the driver: the reset button's service, the GPS
# receiver's power line, the low-power radio's reset line, and the link that hands either's
# serial port to a host.
UNIT_PROGRAMS := keyd:w1700k-key gps:w1700k-gps-line efr32:w1700k-efr32-line port_link:w1700k-port-link
SDR_CLIENTS := probe:mt7996-probe download:mt7996-download stream:mt7996-stream \
	ctl:mt7996-ctl ledd:w1700k-led
module:
	mkdir -p $(SDR)/build
	$(MAKE) -C $(KDIR) ARCH=arm64 CROSS_COMPILE=$(CROSS_COMPILE) M=$(SDR) MO=$(SDR)/build modules
reader:
	mkdir -p $(SDR)/build
	set -e; for pair in $(SDR_CLIENTS) $(UNIT_PROGRAMS); do \
		$(CROSS_COMPILE)gcc -std=c11 -O2 -Wall -Wextra -Werror -static \
			-I$(ROOT)/include/uapi -I$(ROOT)/include $(SDR)/$${pair%%:*}.c -lpthread -lm \
			-o $(SDR)/build/$${pair##*:}; \
	done
