// SPDX-License-Identifier: MIT
// The IQ stream as w1700k-stream serves it on this host (host/stream: shared memory,
// include/w1700k_stream.h), and one reader of it in sample order.
#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

#include "w1700k_stream.h"

// One mapping of the service's object; unmapped when its last user lets go.
class RingView {
public:
    RingView(w1700k_stream_header *h, size_t bytes, uint64_t inode) : h_(h), bytes_(bytes), inode_(inode) {}
    ~RingView();
    RingView(const RingView &) = delete;
    RingView &operator=(const RingView &) = delete;

    w1700k_stream_header *header() const { return h_; }
    uint64_t inode() const { return inode_; }
    bool alive() const { return w1700k_stream_alive(h_) != 0; }

private:
    w1700k_stream_header *h_;
    size_t bytes_;
    uint64_t inode_;
};

// The service's object by name, followed across the service's restarts.
class Ring {
public:
    explicit Ring(std::string name);

    const std::string &name() const { return name_; }
    // Maps the object, or a restarted service's new one. Cheap; call before each use.
    std::shared_ptr<const RingView> view();
    bool alive();

    // What the service is told of the radio (its roll-off correction depends on it), written
    // into every mapping. The correction itself is another consumer's to ask for until
    // set_flatten is called here; from then on this one's wish is written too.
    void set_radio(double sample_rate, double capture_rate, double filter_hz);
    void set_flatten(bool on);

private:
    void write_control(w1700k_stream_header *h);

    const std::string name_;
    std::mutex mutex_;
    std::shared_ptr<const RingView> view_;
    uint64_t sample_rate_ = 0, capture_rate_ = 0, filter_hz_ = 0;
    bool radio_known_ = false, flatten_set_ = false, flatten_ = false;
};

// Reads the stream in order from the moment it is started: the newest slice of the run then in
// progress, and every run after it from its first slice.
class Reader {
public:
    enum Status : long { kTimeout = -1, kGap = -2 };

    explicit Reader(Ring &ring) : ring_(ring) {}

    // From now: the run in progress from its newest slice, or with none, the next one from its
    // first. `rate` is the stream's sample rate, for the sample times.
    void start(double rate);

    // Up to `count` samples into `cs16` (int16 I, Q) or, when that is null, `cf32` (float I, Q,
    // full scale 1.0), waiting up to `timeout_us` for the first. Returns how many, with `time_ns`
    // the first one's time; kTimeout; or kGap: samples before the next ones were lost (never
    // received, or overwritten in the ring before this reader came to them), `time_ns` then
    // being where the stream goes on. Times are on the host's monotonic clock: the moment
    // reading began, advanced by the samples since, received or lost; a new run (the stream
    // restarted) starts again from the time the service saw it begin.
    long read(int16_t *cs16, float *cf32, size_t count, long timeout_us, double rate, long long &time_ns);

    // Since start(): the samples given, the gaps among them, of those the ones where this reader
    // was too slow (the ring overwrote what it had not read), and the furthest it was behind the
    // newest slice, in slices.
    struct Tally {
        uint64_t samples = 0, gaps = 0, lapped = 0, behind = 0;
    };
    const Tally &tally() const { return tally_; }

private:
    long long now_ns() const;
    void rebase(long long at_ns, double rate);
    void skip(uint64_t slices);

    Ring &ring_;
    std::shared_ptr<const RingView> view_;
    uint64_t run_ = 0, index_ = 0;  // the service's run number; the next slice
    unsigned offset_ = 0;           // samples of that slice already given
    bool joined_ = false;           // run_ and index_ are set
    double rate_ = 0;
    long long base_ns_ = 0;         // the time of sample 0 of samples_
    uint64_t samples_ = 0;          // samples since, given or lost
    Tally tally_;
};
