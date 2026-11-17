// SPDX-License-Identifier: MIT
// host/stream/src/awake.cpp for tests/test_host_placement.py:
//   awake_check DEVICE    ask twice, let go, ask again; print the state after each step
#include <cstdio>

#include "awake.h"

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    std::printf("%d", awake::state.load());
    std::printf(" %d:%d", int(awake::hold(argv[1])), awake::state.load());
    awake::needed();                    // the second and every later batch of datagrams: nothing more
    std::printf(" %d", awake::state.load());
    awake::release();
    std::printf(" %d", awake::state.load());
    awake::hold(argv[1]);
    std::printf(" %d\n", awake::state.load());
    return 0;
}
