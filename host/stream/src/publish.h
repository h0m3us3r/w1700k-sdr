// SPDX-License-Identifier: MIT
// w1700k-stream's output: the shared ring every consumer reads (include/w1700k_stream.h), and the
// publisher that fills it from the receive ring, in order, unpacked to complex int16 and, where it
// applies, corrected by the decimation roll-off's inverse (flatten.h).
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "flatten.h"
#include "ingest.h"
#include "w1700k_stream.h"

// The POSIX shared-memory object, created by the service and unlinked when it stops.
class SharedRing {
public:
    // Refuses to start while another live service owns the name.
    SharedRing(const std::string &name, unsigned slices);
    ~SharedRing();
    SharedRing(const SharedRing &) = delete;
    SharedRing &operator=(const SharedRing &) = delete;

    w1700k_stream_header *header() const { return header_; }
    // Consumers can map it from now on: the service receives (its sockets are bound).
    void ready();

private:
    std::string name_;
    size_t bytes_ = 0;
    w1700k_stream_header *header_ = nullptr;
};

class Publisher {
public:
    Publisher(const SampleRing &raw, Ingest &ingest, w1700k_stream_header *out, unsigned workers);
    ~Publisher();
    Publisher(const Publisher &) = delete;
    Publisher &operator=(const Publisher &) = delete;

    void start();

private:
    // Slots [first, last) of `run`, unpacked and, with a correction, filtered. A flush job comes
    // from an idle stream: what follows it has not arrived and counts as zeros.
    struct Job {
        uint64_t sequence = 0, run = 0, first = 0, last = 0;
        const Flatten *flatten = nullptr;
        bool flush = false;
        unsigned channels = 1;  // the run's: 2 lays each slot out by channel
    };
    struct Worker;

    void schedule();
    void work(Worker &w);
    void process(Worker &w, const Job &job);
    enum class Slot { Data, Event, Lost };
    Slot classify(uint64_t run, uint64_t index, bool flush) const;
    bool overrun(uint64_t run, uint64_t index) const;
    void publish_position(uint64_t run, uint64_t newest, unsigned channels);
    const Flatten *correction() const;  // from the control words, nullptr: none applies
    void report();                      // heartbeat and counters into the header

    const SampleRing &raw_;
    Ingest &ingest_;
    w1700k_stream_header *out_;
    unsigned worker_count_;

    std::mutex mutex_;
    std::condition_variable work_ready_, job_done_;
    std::deque<Job> jobs_;
    std::map<uint64_t, Job> done_;  // finished, waiting for their turn to be published
    unsigned busy_ = 0;
    bool stopping_ = false;

    std::atomic<uint64_t> lost_{0}, overruns_{0}, flattened_{0}, saturated_{0};
    std::vector<std::unique_ptr<Worker>> workers_;
    std::thread scheduler_;
};
