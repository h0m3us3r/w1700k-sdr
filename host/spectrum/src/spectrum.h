// SPDX-License-Identifier: MIT
// Live power spectrum of the stream: windowed FFTs of evenly spaced blocks of samples, combined
// per frame and converted to dBFS. Frames are computed in parallel, each by one worker, straight
// from the stream's shared ring (stream.h).
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "stream.h"

struct SpectrumSettings {
    unsigned size = 262144;    // FFT points: a power of two, 512..262144
    std::string window = "blackman-harris-4";
    double rate = 100;         // frames per second (of sample time)
    unsigned averaging = 0;    // FFT blocks per frame; 0: every sample
    bool peak = true;          // combine blocks by maximum, not mean power
    bool remove_dc = false;    // subtract each block's mean first
    // Of a two-channel stream: the channel shown, 0 or 1, or kBothChannels: a frame of each over
    // the same samples, channel 0's first (SpectrumFrame::channel says which a frame is).
    unsigned channel = 0;
    static constexpr unsigned kBothChannels = 2;

    // Throws std::runtime_error naming the first invalid field.
    void validate() const;
};

// dBFS of a frame's power value.
float spectrum_db(float power);

// FFT window names, in the order the UI offers them, and each one's
// equivalent noise bandwidth in bins.
const std::vector<std::string> &spectrum_windows();
double spectrum_window_bandwidth(const std::string &window);

// What the samples are: the LO they were mixed down with, and their rate.
struct Tuning {
    double center_hz = 0;
    double sample_rate = 0;
    bool operator==(const Tuning &o) const { return center_hz == o.center_hz && sample_rate == o.sample_rate; }
    bool operator!=(const Tuning &o) const { return !(*this == o); }
};

struct SpectrumFrame {
    uint64_t run = 0;          // stream run the samples came from
    uint64_t index = 0;        // sample index of the frame's first sample
    double center_hz = 0;      // LO
    double sample_rate = 0;    // span of the frame, Hz
    unsigned blocks = 0;       // FFT blocks combined
    bool peak = false;
    // The channel of the stream the frame is of (0 of a one-channel stream). Frames say it so
    // that those of two channels can travel together: a view of both is two frames over the
    // same samples (the same run and index), one of each channel.
    unsigned channel = 0;
    // Power per bin relative to a full-scale tone (1 = 0 dBFS), from center - rate/2 upwards.
    // Linear, so that frames merge and reduce to a view's bins without a logarithm per FFT bin;
    // spectrum_db() converts what is sent.
    std::vector<float> power;
};

class SpectrumEngine {
public:
    using Output = std::function<void(std::shared_ptr<const SpectrumFrame>)>;

    // output: called on a worker thread, in frame order.
    SpectrumEngine(const StreamClient &stream, const Tuning &tuning, unsigned workers, Output output);
    ~SpectrumEngine();
    SpectrumEngine(const SpectrumEngine &) = delete;
    SpectrumEngine &operator=(const SpectrumEngine &) = delete;

    void configure(const SpectrumSettings &settings);  // validated by the caller
    SpectrumSettings settings() const;

    // The tuning was reported changed: it applies from a little past the newest slice received,
    // since slices from before the change may still be on their way. Frames with samples on
    // both sides of a change are dropped, never labelled with either tuning. Nothing happens
    // when the tuning is unchanged and no change is in progress.
    void set_tuning(const Tuning &tuning);
    // A change has been requested: from the newest slice on, frames are dropped until
    // set_tuning() says what the samples are again.
    void begin_change();
    Tuning tuning() const;  // the latest, which a change in progress has not replaced yet
    // The tuning that held over slices [first, last] of `run`, if one did throughout.
    std::optional<Tuning> tuning_over(uint64_t run, uint64_t first, uint64_t last) const;

    struct Stats {
        uint64_t frames = 0, blocks = 0;
        uint64_t frames_retuned = 0;  // frames dropped because they span or fall in a change
        uint64_t pairs_used = 0;      // samples that went into FFT blocks
        uint64_t pairs_dropped = 0;   // samples of frames skipped while the workers were busy
        double share = 1;             // of the blocks "every sample" takes, the share the workers keep up with
        uint64_t pairs_missing = 0;   // samples of blocks left out: a slice lost, overwritten or an event
        uint64_t slices_lost = 0;     // slices that never arrived
        uint64_t slices_skipped = 0;  // slices passed over while the scheduler was behind
        uint64_t backlog = 0;         // slices received and not yet reached by the frontier
        unsigned queued = 0, workers = 0;
    };
    Stats stats() const;

private:
    struct Plan;
    struct Transform;
    struct Job;
    struct Worker;
    // A tuning and the slot it applies from (ordered by run, then slot index).
    struct Segment {
        uint64_t run = 0, from = 0;
        bool valid = true;  // false: a change in progress
        Tuning tuning;
    };

    void schedule();
    void work(Worker &worker);
    void finish(uint64_t sequence, std::shared_ptr<SpectrumFrame> frame);
    std::shared_ptr<const Transform> transform(unsigned size, const std::string &window);
    void refine();  // the thread that measures plans
    void add_segment(Segment segment);  // with mutex_ held

    const StreamClient &stream_;
    Output output_;

    mutable std::mutex mutex_;
    std::condition_variable work_ready_;
    std::deque<Job> jobs_;
    SpectrumSettings settings_;
    uint64_t settings_version_ = 1;
    bool stopping_ = false;
    Stats stats_;
    Segment latest_;                 // the newest segment
    std::deque<Segment> pending_;    // not yet seen by the scheduler
    std::deque<Segment> history_;    // recent segments, for tuning_over()

    std::mutex results_mutex_;
    std::map<uint64_t, std::shared_ptr<SpectrumFrame>> results_;
    uint64_t emit_next_ = 0;

    std::mutex transforms_mutex_;
    std::map<std::pair<unsigned, std::string>, std::shared_ptr<const Transform>> transforms_;
    std::map<unsigned, std::shared_ptr<const Plan>> plans_;  // by size; windows share them
    std::deque<unsigned> refine_;                            // sizes whose plan is only estimated
    std::condition_variable refine_ready_;
    bool refine_stop_ = false;
    std::thread refiner_;

    std::vector<std::unique_ptr<Worker>> workers_;
    std::thread scheduler_;
};
