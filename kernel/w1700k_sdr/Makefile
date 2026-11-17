# SPDX-License-Identifier: GPL-2.0-only
obj-m += w1700k_sdr.o
w1700k_sdr-y := pci.o mmio.o ownership.o dma-ring.o fwdl.o fwdl_wire.o \
		fwdl-image.o wfdma.o export.o pump.o led.o
ccflags-y += -I$(src)/../../include/uapi
ccflags-y += -I$(src)/../../include
