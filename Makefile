# SPDX-License-Identifier: MIT
#
#   make          build everything below, in dependency order
#   make check    run the test suite; its report is tests/build/report.html
#   make sweep    run every stream mode on a real unit, briefly, and add that to the report
#
# Unit images land in platform/build/images/, MT7996 firmware in firmware/build/,
# host programs under host/. The first build downloads about 500 MB of sources and
# compiles a cross toolchain: about ten minutes on a 20-core host plus downloads;
# later builds are incremental.
# JOBS=N limits parallel compile jobs (default: all CPUs).

PYTHON ?= python3
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: all toolchain linux sdr rootfs loader dumper restorer images firmware host check sweep bench \
	clean distclean

all: images dumper restorer firmware host

# Buildroot cross toolchain, target packages and the host tools the later steps use.
toolchain:
	$(PYTHON) platform/rootfs/build.py toolchain

# Linux Image and device tree.
linux: toolchain
	$(PYTHON) platform/linux/build.py

# The w1700k_sdr module and the unit-side clients, built against that kernel.
sdr: linux
	$(MAKE) -C platform/linux module reader

# The read-only root filesystem, with the module and clients installed.
rootfs: sdr
	$(PYTHON) platform/rootfs/build.py image

# Our U-Boot, which stock U-Boot loads from NAND. Also builds mkimage.
loader: toolchain
	$(PYTHON) platform/loader/build.py

# The system and recovery FITs, the root and the durable images (one per NAND part).
images: rootfs loader
	$(PYTHON) platform/images.py

# The write-free NAND dumper used for the backup before installation.
dumper: toolchain
	$(PYTHON) platform/dumper/build.py

# The restorer, which writes a backup back to return a unit to stock firmware.
restorer: toolchain
	$(PYTHON) platform/restorer/build.py

# MT7996 ROM patch, WM/WA firmware and the stream programs.
firmware:
	$(MAKE) -C firmware

# The stream service, the spectrum web UI server, the IQ recorder and, where SoapySDR's
# headers are installed, the SoapySDR module.
host:
	$(MAKE) -C host/stream
	$(MAKE) -C host/spectrum
	$(MAKE) -C host/recv
	@if pkg-config --exists SoapySDR 2>/dev/null || [ -e /usr/include/SoapySDR/Device.hpp ]; then \
		$(MAKE) -C host/soapy; \
	else \
		echo "host/soapy: SoapySDR headers not found, not built"; \
	fi

# Every test (none needs a unit), reported on the terminal as unittest does and as a page.
check:
	$(PYTHON) tools/check_hw_names.py
	$(PYTHON) tools/test_report.py

# What the tests cannot: each sample rate, packing and band on a unit that is reachable,
# with w1700k-stream running on this host. About five minutes.
sweep:
	$(PYTHON) tools/unit_sweep.py

# Watch the stream this host is taking in, until Ctrl-C, and say how it fared and under what load.
bench:
	$(PYTHON) tools/host_bench.py

# Remove build outputs but keep downloads and prepared sources.
clean:
	rm -rf platform/build/buildroot platform/build/linux platform/build/loader \
		platform/build/dumper platform/build/restorer platform/build/mkimage \
		platform/build/bootstrap platform/build/images platform/build/rootfs-overlay \
		platform/build/logs \
		kernel/w1700k_sdr/build

# Remove everything the build created, downloads included.
distclean:
	rm -rf platform/build kernel/w1700k_sdr/build
