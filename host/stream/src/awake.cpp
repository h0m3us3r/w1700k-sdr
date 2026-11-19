// SPDX-License-Identifier: MIT
#include "awake.h"

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>

#include <fcntl.h>
#include <unistd.h>

namespace awake {

std::atomic<int> state{kIdle};

namespace {

constexpr int32_t kLatencyUs = 100;
std::mutex lock;
int fd = -1;

}  // namespace

bool hold(const std::string &device)
{
    std::lock_guard<std::mutex> guard(lock);
    if (state.load(std::memory_order_relaxed) != kIdle) return fd >= 0;
    fd = open(device.c_str(), O_WRONLY | O_CLOEXEC);
    if (fd >= 0 && write(fd, &kLatencyUs, sizeof(kLatencyUs)) != ssize_t(sizeof(kLatencyUs))) {
        const int error = errno;
        close(fd);
        fd = -1;
        errno = error;
    }
    if (fd < 0) {
        static bool said = false;
        if (!said)
            std::fprintf(stderr,
                         "w1700k-stream: this host is not kept out of deep idle while a stream arrives (%s: %s); "
                         "a slow stream may lose slices where the host sleeps deeply (sudo init/host/link keep ...)\n",
                         device.c_str(), std::strerror(errno));
        said = true;
        state.store(kRefused, std::memory_order_relaxed);
        return false;
    }
    state.store(kHeld, std::memory_order_relaxed);
    return true;
}

void release()
{
    std::lock_guard<std::mutex> guard(lock);
    if (fd >= 0) close(fd);
    fd = -1;
    state.store(kIdle, std::memory_order_relaxed);
}

}  // namespace awake
