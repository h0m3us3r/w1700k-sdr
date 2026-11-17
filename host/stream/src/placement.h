// SPDX-License-Identifier: MIT
// Where this process runs beside the network stack. An interface with one receive queue (a USB
// adapter) hands every datagram to the kernel on one CPU, the one its interrupt is bound to, and
// at 127,000 datagrams a second (320 Msps on 5 Gbit/s) that CPU has nothing to spare: whatever
// else runs there delays the next read of the adapter, which then drops what it cannot hold.
// Measured on an 8-CPU host with the packets steered away from that CPU (init/host/link): 4.8
// datagrams a second missed with this service's threads allowed there, 1.6 with them kept off.
#pragma once

#include <string>

namespace placement {

// The CPU that takes the packets of the interface named `interface`, where one CPU takes them
// all: the one its interrupt is delivered to now (the kernel may move it, so ask again). -1 for
// an interface with several receive queues, or without an interrupt of its own or of the
// controller it hangs from (a USB controller's is also looked for in /proc/interrupts, under
// the names of its buses: one that is no PCI device shows none in sysfs). `sys` and `proc` are /sys and /proc (other roots in tests).
int receive_cpu(const std::string &interface, const std::string &sys = "/sys", const std::string &proc = "/proc");

// The name of the interface that holds the IPv4 address `ip`; empty: none.
std::string interface_of(const std::string &ip);

// Every thread of this process on the CPUs it was started with, less `cpu` (-1: all of them).
// Nothing happens where that would leave fewer than three CPUs.
void keep_off(int cpu);

}  // namespace placement
