// SPDX-License-Identifier: MIT
// The unit's UDP export received into a ring of raw sample words, w1700k-stream's reorder buffer.
// Each datagram carries one completion of the unit's DMA ring, numbered by `seq`, or a piece of
// one: a sample slice, raw (2048 words), packed to 28 bits (7168 bytes) or reduced to fewer bits
// with a scale (include/hw/mt7996/iq_reduce.h), or a short firmware event that shares the ring.
// A sample word holds 14-bit I in bits [29:16] and Q in [13:0]; every slice is filed as such words.
#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <thread>
#include <vector>

constexpr unsigned kSliceSamples = 2048;
constexpr unsigned kLap = 128;  // slices in the unit's 1 MiB capture ring

// Slots are indexed by completion number, so a firmware event occupies a slot without samples.
// Events consume no sample time: the slices either side of one are contiguous.
class SampleRing {
public:
    // capacity_slices: a power of two.
    explicit SampleRing(unsigned capacity_slices);

    unsigned capacity_slices() const { return slices_; }

    // Writer: slice `index` of run `run`. Any thread.
    void put(uint64_t run, uint64_t index, const uint8_t *words);
    void put_packed(uint64_t run, uint64_t index, const uint8_t *packed);
    void put_reduced(uint64_t run, uint64_t index, unsigned format, unsigned scale, const uint8_t *reduced);
    bool put_radio14(uint64_t run, uint64_t index, const uint8_t *chunk);
    // After slot `index` was filed reduced: whether a radio chunk here or beside it was cut under
    // a shift too small for what the unit measured in the slice next to it (its samples may wrap).
    bool suspect(uint64_t run, uint64_t index) const;
    void put_event(uint64_t run, uint64_t index);  // a completion that is not samples

    // Readers. A slot's tag names the run and slice it holds; a reader checks it before and after
    // reading the slice's words, so a slot overwritten meanwhile is noticed (a seqlock).
    uint64_t tag(uint64_t index) const { return tags_[index & (slices_ - 1)].load(std::memory_order_acquire); }
    static uint64_t tag_of(uint64_t run, uint64_t index) { return (run << 40) | (index + 1); }
    static uint64_t event_tag_of(uint64_t run, uint64_t index) { return tag_of(run, index) | kEventBit; }
    const uint32_t *slice(uint64_t index) const { return &words_[(index & (slices_ - 1)) * kSliceSamples]; }

private:
    static constexpr uint64_t kEventBit = uint64_t(1) << 63;
    void claim(uint64_t index);
    // What a reduced slot was cut under, for the check of a split stream's radio chunks against
    // their neighbours: kRadio | shift, or the unit's scale code; kNone otherwise.
    static constexpr uint16_t kNone = 0xffff, kRadio = 0x8000;
    std::unique_ptr<std::atomic<uint16_t>[]> cut_;
    unsigned slices_;
    std::vector<uint32_t> words_;
    std::unique_ptr<std::atomic<uint64_t>[]> tags_;
};

// Receives the stream on IP:PORT with several threads (SO_REUSEPORT; the unit cycles its source
// port so the flows spread over them) and files every completion into the ring by its sequence
// number. A run starts with the first completion after a second of silence, after a jump in
// sequence, or after restart_run().
class Ingest {
public:
    struct Stats {
        uint64_t run = 0;                // current run, 1.. (0: none yet)
        uint64_t datagrams = 0, bytes = 0;
        uint64_t slices = 0, packed = 0; // sample slices filed, and those of them packed to 28 bits
        uint64_t reduced = 0;            // and those reduced to fewer bits, by the unit's SoC or by the radio
        uint64_t suspect = 0;            // radio chunks beside a slice louder than their shift holds
        uint64_t events = 0;             // completions that are not samples (firmware events)
        uint64_t pieces = 0;             // slices sent in pieces that did not all arrive, in order
        uint64_t bad = 0;                // wrong magic or length, an index far ahead of its stream, or
                                         // (with a unit named) another sender
        uint64_t late = 0;               // older than the run's start or than the ring holds
        uint64_t before_start = 0;       // in a run's first second, from before it (joined late)
        uint64_t duplicates = 0;
        uint64_t socket_drops = 0;       // SO_RXQ_OVFL, summed over the sockets
        // The export's integrity: words of raw slices with spare bits set, and slices or 8-sample
        // lines equal to the same slice or line one capture-ring lap (kLap completions) earlier.
        uint64_t spare_words = 0, stale_slices = 0, stale_lines = 0;
        uint64_t newest = 0;             // highest slot index + 1 of the run
        double run_started = 0;          // steady seconds
        double last_any = 0;
    };
    struct Position {
        uint64_t run = 0, newest = 0;
    };

    // `unit_ip`: the only sender heard, when not empty.
    Ingest(SampleRing &ring, const std::string &bind_ip, unsigned port, unsigned threads,
           const std::string &unit_ip = std::string());
    ~Ingest();
    Ingest(const Ingest &) = delete;
    Ingest &operator=(const Ingest &) = delete;

    void start();  // binds the sockets and starts receiving
    Stats stats() const;
    uint64_t run() const { return run_.load(std::memory_order_acquire); }
    uint64_t newest() const { return position().newest; }
    Position position() const;  // run and newest, read consistently
    void restart_run();         // the next completion starts a new run (the stream was restarted)

private:
    struct Worker;
    void work(Worker &w);
    void take(Worker &w, const uint8_t *data, size_t n, bool fresh, uint64_t t);
    size_t record(Worker &w, const uint8_t *data, size_t n, uint64_t t);
    void check(Worker &w, uint64_t run, uint64_t index);

    SampleRing &ring_;
    const std::string bind_ip_;
    uint32_t unit_ = 0;                  // the unit's address in network order; 0: any sender
    const unsigned port_, threads_;
    std::atomic<bool> stopping_{false};
    std::vector<std::unique_ptr<Worker>> workers_;

    // The run: its first sequence number, and the newest slot filled, kept with the run it is of
    // (ingest.cpp, `placed`).
    std::atomic<uint64_t> run_{0}, base_{0}, newest_{0};
    std::atomic<uint64_t> started_ns_{0}, last_ns_{0};
    std::atomic<bool> new_run_{false};
};
