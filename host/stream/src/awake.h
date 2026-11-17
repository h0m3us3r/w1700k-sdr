// SPDX-License-Identifier: MIT
// The host kept out of its deep idle states while a stream arrives. A stream at 40 or 80 Msps
// comes in clumps with idle stretches between them, long enough for a host to put every CPU to
// sleep and then enter its deepest, whole-system state; a USB adapter holds under a millisecond
// of stream, and misses datagrams while such a host wakes. Measured on an arm64 host whose CPU
// sleep state takes 1.3 ms to leave: 0.3 to 0.9 % of the 16-bit format's slices lost at 40 Msps,
// and next to none with any one CPU awake. Faster streams keep a host awake by themselves.
//
// The kernel's CPU latency request (/dev/cpu_dma_latency: no CPU idles deeper than a state it
// leaves within the time written, for as long as the file is held open) is made when datagrams
// start to arrive and dropped when they stop, so the host idles as it likes between streams.
// The file is root's unless `init/host/link` has given it to the user.
#pragma once

#include <atomic>
#include <string>

namespace awake {

enum State { kIdle, kHeld, kRefused };
extern std::atomic<int> state;

// From now until release(): no CPU idles deeper than a state it leaves in 100 us. False, and
// kRefused until release(), where `device` cannot be opened or written (said once on stderr).
bool hold(const std::string &device = "/dev/cpu_dma_latency");

// The host may idle as it likes again, and the next hold() tries afresh.
void release();

// For a receive thread, each time datagrams arrived: one load unless this is the first.
inline void needed()
{
    if (state.load(std::memory_order_relaxed) == kIdle) hold();
}

}  // namespace awake
