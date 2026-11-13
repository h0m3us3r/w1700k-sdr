// SPDX-License-Identifier: MIT
// The IQ stream as w1700k-stream serves it to every consumer on this host (host/stream: shared
// memory, include/w1700k_stream.h): slices of complex int16 samples in a ring, published in order.
// The client follows the service across restarts: each mapping is an epoch, and a run is named by
// its epoch and the service's run number, so a restarted service's run 1 is a new run here.
#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

#include "w1700k_stream.h"

constexpr unsigned kSliceSamples = W1700K_STREAM_SLICE;

// One mapping of the service's object; unmapped when the last user lets go.
class StreamView {
public:
    StreamView(w1700k_stream_header *h, size_t bytes, uint64_t inode, uint64_t epoch)
        : h_(h), bytes_(bytes), inode_(inode), epoch_(epoch) {}
    ~StreamView();
    StreamView(const StreamView &) = delete;
    StreamView &operator=(const StreamView &) = delete;

    w1700k_stream_header *header() const { return h_; }
    uint64_t inode() const { return inode_; }
    uint64_t epoch() const { return epoch_; }
    uint64_t capacity() const { return h_->ring_slices; }
    uint64_t tag(uint64_t index) const
    {
        return __atomic_load_n(&w1700k_stream_tags(h_)[index & (h_->ring_slices - 1)], __ATOMIC_ACQUIRE);
    }
    const int16_t *slice(uint64_t index) const { return w1700k_stream_slice(h_, index); }

    // Run identifiers here: epoch << 32 | the service's run.
    uint64_t run_id(uint64_t service_run) const { return (epoch_ << 32) | service_run; }
    static uint64_t service_run(uint64_t run_id) { return run_id & 0xffffffffu; }
    // Slot kinds against a run identifier.
    static bool holds_samples(uint64_t tag, uint64_t run_id, uint64_t index)
    {
        return (tag & ~W1700K_STREAM_TAG_FLAT) == w1700k_stream_tag(service_run(run_id), index);
    }
    static bool holds_event(uint64_t tag, uint64_t run_id, uint64_t index)
    {
        return tag == (w1700k_stream_tag(service_run(run_id), index) | W1700K_STREAM_TAG_EVENT);
    }
    static bool holds_loss(uint64_t tag, uint64_t run_id, uint64_t index)
    {
        return tag == (w1700k_stream_tag(service_run(run_id), index) | W1700K_STREAM_TAG_LOST);
    }

private:
    w1700k_stream_header *h_;
    size_t bytes_;
    uint64_t inode_, epoch_;
};

class StreamClient {
public:
    explicit StreamClient(std::string name);

    const std::string &name() const { return name_; }
    // Maps the service's object, again after it restarted, and puts the control back. Call often
    // (cheap); not from two threads at once.
    void refresh();
    std::shared_ptr<const StreamView> view() const;  // nullptr: no service
    bool alive() const;
    // The CPU the service asks its consumers to stay off (w1700k_stream.h, receive_cpu); -1: none.
    int receive_cpu() const;

    struct Position {
        uint64_t run = 0, newest = 0;  // run: a run identifier (StreamView::run_id), 0: none
    };
    Position position() const;

    struct Stats {
        uint64_t run = 0, ring_slices = 0;  // run: the service's number, 0: none yet
        uint64_t datagrams = 0, bytes = 0, slices = 0, packed = 0, events = 0, lost = 0;
        uint64_t pieces = 0, bad = 0, late = 0, before_start = 0, duplicates = 0, socket_drops = 0;
        uint64_t spare_words = 0, stale_slices = 0, stale_lines = 0, flattened = 0, saturated = 0, overruns = 0;
        double run_started = 0, last_any = 0;  // steady seconds
        bool flatten_active = false;
    };
    Stats stats() const;

    // Control, kept here and written again into a restarted service.
    void restart_run();
    void set_radio(double sample_rate, double capture_rate, double filter_hz);
    void set_flatten(bool on);
    bool flatten() const;

private:
    void write_control(w1700k_stream_header *h);

    const std::string name_;
    mutable std::mutex mutex_;
    std::shared_ptr<const StreamView> view_;
    uint64_t epoch_ = 0;
    uint64_t flatten_ = 0, sample_rate_ = 0, capture_rate_ = 0, filter_hz_ = 0;
};
