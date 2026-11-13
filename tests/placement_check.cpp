// SPDX-License-Identifier: MIT
// host/stream/src/placement.cpp for tests/test_host_placement.py:
//   placement_check cpu INTERFACE SYS PROC    the CPU that takes the interface's packets, or -1
//   placement_check off CPU                   keep off CPU, then print the CPUs this thread may use
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <sched.h>

#include "placement.h"

int main(int argc, char **argv)
{
    if (argc == 5 && !std::strcmp(argv[1], "cpu")) {
        std::printf("%d\n", placement::receive_cpu(argv[2], argv[3], argv[4]));
    } else if (argc == 3 && !std::strcmp(argv[1], "off")) {
        cpu_set_t set;
        placement::keep_off(std::atoi(argv[2]));
        placement::keep_off(-1);
        placement::keep_off(std::atoi(argv[2]));
        sched_getaffinity(0, sizeof(set), &set);
        for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
            if (CPU_ISSET(cpu, &set)) std::printf("%d ", cpu);
        std::printf("\n");
    } else {
        return 2;
    }
    return 0;
}
