// SPDX-License-Identifier: MIT
// w1700k-stream: receives the W1700K's UDP export and serves it to every consumer on this host
// through shared memory (include/w1700k_stream.h): in order, unpacked to complex int16, and on
// request corrected for the radio's decimation roll-off (flatten.h).
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <thread>

#include <sys/resource.h>

#include "ingest.h"
#include "awake.h"
#include "placement.h"
#include "publish.h"

namespace {

// The reorder ring's default: 1 GiB, 0.84 s at 320 Msps. The publisher waits 250 ms for a late
// slice (publish.cpp), and never while more than half the ring is newer; at 320 Msps a quarter of
// a second is 39,000 slices, more than the 256 MiB this ring used to be could hold in wait.
constexpr unsigned kReceiveMib = 1024;
constexpr int kNice = -10;

std::atomic<bool> stop_requested{false};
void on_signal(int) { stop_requested = true; }

void usage()
{
    std::fprintf(stderr,
                 "usage: w1700k-stream [options]\n"
                 "  --bind IP         the stream's destination address on this host (default 10.77.0.10)\n"
                 "  --port N          its UDP port (default 7700)\n"
                 "  --unit IP         take the stream from this sender alone (default: from any; the stream's\n"
                 "                    link is meant to hold the unit and this host and nothing else)\n"
                 "  --rx-threads N    UDP sockets and receive threads (default 8)\n"
                 "  --workers N       unpacking and correction threads (default 6)\n"
                 "  --ring-mib N      the shared sample ring, a power of two (default 512: 0.42 s at 320 Msps)\n"
                 "  --receive-mib N   the reorder ring, a power of two (default 1024: a late slice is waited\n"
                 "                    for 250 ms, never past half of the ring)\n"
                 "  --name NAME       the shared-memory object (default /w1700k-stream)\n"
                 "  --report S        a JSON status line every S seconds (default 0: none)\n"
                 "  --share-irq-cpu   run also on the CPU that takes the interface's packets (default: where\n"
                 "                    one CPU takes them all, as with a USB adapter, stay off it)\n"
                 "  --deep-idle       let the host idle as deeply as it likes while a stream arrives (default:\n"
                 "                    ask the kernel to keep the CPUs out of deep idle for as long as one does)\n"
                 "  --own-priority    run at the priority it was started with (default: nice -10, where the\n"
                 "                    user may, so that a busy host does not starve the receive threads)\n");
}

unsigned positive(const std::string &option, const std::string &text, unsigned long max)
{
    size_t used = 0;
    unsigned long v = 0;
    try {
        v = std::stoul(text, &used);
    } catch (const std::logic_error &) {
        used = 0;
    }
    if (!used || used != text.size() || v == 0 || v > max)
        throw std::runtime_error(option + " needs an integer 1.." + std::to_string(max) + ", not \"" + text + "\"");
    return unsigned(v);
}

}  // namespace

int main(int argc, char **argv)
{
    std::string bind = "10.77.0.10", name = W1700K_STREAM_NAME, unit;
    unsigned port = 7700, rx_threads = 8, workers = 6, ring_mib = 512, receive_mib = kReceiveMib, report = 0;
    bool share_irq_cpu = false, deep_idle = false, own_priority = false;
    try {
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            auto value = [&]() -> std::string {
                if (i + 1 >= argc) throw std::runtime_error(arg + " needs a value");
                return argv[++i];
            };
            if (arg == "--bind") bind = value();
            else if (arg == "--port") port = positive(arg, value(), 65535);
            else if (arg == "--unit") unit = value();
            else if (arg == "--rx-threads") rx_threads = positive(arg, value(), 64);
            else if (arg == "--workers") workers = positive(arg, value(), 64);
            else if (arg == "--ring-mib") ring_mib = positive(arg, value(), 1u << 16);
            else if (arg == "--receive-mib") receive_mib = positive(arg, value(), 1u << 14);
            else if (arg == "--name") name = value();
            else if (arg == "--report") report = positive(arg, value(), 3600);
            else if (arg == "--share-irq-cpu") share_irq_cpu = true;
            else if (arg == "--deep-idle") deep_idle = true;
            else if (arg == "--own-priority") own_priority = true;
            else if (arg == "-h" || arg == "--help") {
                usage();
                return 0;
            } else {
                throw std::runtime_error("unknown option " + arg);
            }
        }
        if (ring_mib & (ring_mib - 1)) throw std::runtime_error("--ring-mib must be a power of two");
        if (receive_mib & (receive_mib - 1) || receive_mib < 64)
            throw std::runtime_error("--receive-mib must be a power of two, at least 64");
    } catch (const std::exception &e) {
        std::fprintf(stderr, "w1700k-stream: %s\n", e.what());
        usage();
        return 2;
    }

    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    // Before any thread is started: each inherits it. On a host with every CPU busy the receive
    // threads at the ordinary priority were starved (2.1 % of a 320 Msps stream lost, 61,000
    // datagrams dropped at the sockets in a minute); at -10, 0.008 to 0.08 % (README.md).
    // Lowering nice is root's unless the user's limit allows it, which `init/host/link keep` sets.
    if (!own_priority && getpriority(PRIO_PROCESS, 0) > kNice && setpriority(PRIO_PROCESS, 0, kNice))
        std::fprintf(stderr,
                     "w1700k-stream: running at the ordinary priority (nice %d: %s); a busy host may starve it "
                     "(sudo init/host/link keep ..., then log in again)\n",
                     kNice, std::strerror(errno));
    try {
        SharedRing shared(name, ring_mib * 128);
        SampleRing received(receive_mib * 128);
        Ingest ingest(received, bind, port, rx_threads, unit);
        Publisher publisher(received, ingest, shared.header(), workers);
        if (deep_idle) awake::state.store(awake::kRefused, std::memory_order_relaxed);  // as if it could not ask
        publisher.start();
        ingest.start();
        shared.ready();
        std::fprintf(stderr, "w1700k-stream: serving %s:%u as %s (%u MiB)\n", bind.c_str(), port, name.c_str(), ring_mib);
        auto last = std::chrono::steady_clock::now();
        // Looked at again every two seconds: the interface may appear, or its interrupt be
        // moved, after this service started.
        int avoided = -1;
        unsigned tick = 0;
        // The receive threads ask for the host to be kept awake when datagrams arrive; two
        // seconds without any, and it may sleep as it likes again.
        uint64_t arrived = 0;
        unsigned quiet = 0;
        while (!stop_requested) {
            const uint64_t datagrams = ingest.stats().datagrams;
            quiet = datagrams == arrived ? quiet + 1 : 0;
            arrived = datagrams;
            if (quiet == 20 && !deep_idle && awake::state.load(std::memory_order_relaxed) != awake::kIdle)
                awake::release();
            if (!share_irq_cpu && tick++ % 20 == 0) {
                const std::string interface = placement::interface_of(bind);
                const int cpu = placement::receive_cpu(interface);
                if (cpu != avoided) {
                    placement::keep_off(cpu);
                    if (cpu >= 0)
                        std::fprintf(stderr, "w1700k-stream: staying off CPU %d, which takes every packet of %s\n", cpu,
                                     interface.c_str());
                    else
                        std::fprintf(stderr, "w1700k-stream: on every CPU again\n");
                    avoided = cpu;
                    __atomic_store_n(&shared.header()->receive_cpu, uint64_t(cpu + 1), __ATOMIC_RELEASE);
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            if (!report || std::chrono::steady_clock::now() - last < std::chrono::seconds(report)) continue;
            last = std::chrono::steady_clock::now();
            const w1700k_stream_header *h = shared.header();
            uint64_t run, newest;
            w1700k_stream_position(h, &run, &newest);
            std::printf("{\"run\":%" PRIu64 ",\"newest\":%" PRIu64 ",\"datagrams\":%" PRIu64 ",\"slices\":%" PRIu64
                        ",\"packed\":%" PRIu64 ",\"reduced\":%" PRIu64 ",\"suspect\":%" PRIu64 ",\"events\":%" PRIu64 ",\"lost\":%" PRIu64
                        ",\"overruns\":%" PRIu64 ",\"pieces\":%" PRIu64 ",\"late\":%" PRIu64
                        ",\"duplicates\":%" PRIu64 ",\"bad\":%" PRIu64 ",\"socket_drops\":%" PRIu64
                        ",\"spare_words\":%" PRIu64 ",\"stale_slices\":%" PRIu64 ",\"stale_lines\":%" PRIu64
                        ",\"flatten_active\":%" PRIu64 ",\"flattened\":%" PRIu64 ",\"saturated\":%" PRIu64 "}\n",
                        run, newest, h->datagrams, h->slices, h->packed, h->reduced, h->suspect, h->events, h->lost, h->overruns,
                        h->pieces,
                        h->late, h->duplicates,
                        h->bad, h->socket_drops, h->spare_words, h->stale_slices, h->stale_lines, h->flatten_active,
                        h->flattened, h->saturated);
            std::fflush(stdout);
        }
    } catch (const std::exception &e) {
        std::fprintf(stderr, "w1700k-stream: %s\n", e.what());
        return 1;
    }
    return 0;
}
