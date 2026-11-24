// SPDX-License-Identifier: MIT
#include "spectrum.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>

#include <fcntl.h>
#include <fftw3.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "../../stream/src/apart.h"

namespace {

constexpr double kFullScale = 8192;    // 14-bit two's complement components
constexpr uint64_t kHorizon = 2048;    // slices: a slice still missing this far back is lost
// Slices that may still arrive from before a reported tuning change: reordering across the
// receive threads, and the unit's export lag behind its DMA ring.
constexpr uint64_t kSettleSlices = 1024;
constexpr size_t kHistory = 256;       // segments kept for tuning_over()
// The FFT workers take what CPU is left: they run at this nice level, so the stream service's
// receive threads, the web thread and a browser on the same host are served first. A spectrum
// that covers fewer samples is a small loss; a receive thread kept waiting loses slices.
constexpr int kWorkerNice = 15;
// "Every sample" on a host that cannot transform them all: frames keep coming at their rate with
// a share of their blocks, rather than whole frames being skipped. The share eases down while
// frames find the queue half full, falls by kShareDown when one finds it full (and is skipped),
// and recovers by kShareUp per frame that finds it empty.
constexpr double kShareEase = 0.97, kShareDown = 0.85, kShareUp = 0.01;

// Cosine-sum windows: w[n] = sum_k (-1)^k a_k cos(2 pi k n / N), periodic.
const std::vector<std::pair<std::string, std::vector<double>>> &window_table()
{
    static const std::vector<std::pair<std::string, std::vector<double>>> table = {
        {"rectangular", {1}},
        {"hann", {0.5, 0.5}},
        {"hamming", {0.54, 0.46}},
        {"blackman", {0.42, 0.5, 0.08}},
        {"blackman-harris-4", {0.35875, 0.48829, 0.14128, 0.01168}},
        {"blackman-harris-7",
         {0.27105140069342, 0.43329793923448, 0.21812299954311, 0.06592544638803, 0.01081174209837,
          0.00077658482522, 0.00001388721735}},
        {"flat-top", {0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368}},
    };
    return table;
}

std::mutex planner;  // FFTW's planner is not thread-safe; executing a plan is

// A plan FFTW has measured runs the largest sizes about 40 % faster than one it estimates. It
// takes seconds to find where FFTW has a cycle counter to time with and minutes where it has
// none (arm64 as distributions build it: a minute at 32,768 points on an 8-core host), and with
// a time limit that cuts the search short it can come out slower than the estimate. So each
// size's measured plan is found once, with no limit, by a process of its own that goes on if the
// server ends (`w1700k-spectrum --measure-plan N`; apart.h), and kept here for later runs
// (FFTW's "wisdom"). Measured in a thread of the server, as it was, the server could not be
// stopped until the measurement ended (106 s on that host, at its limit, and not kept), and
// planning another size waited as long. "": no cache directory.
std::string wisdom_path(bool create)
{
    std::string dir;
    if (const char *cache = getenv("XDG_CACHE_HOME"); cache && *cache) dir = cache;
    else if (const char *home = getenv("HOME"); home && *home) dir = std::string(home) + "/.cache";
    else return "";
    if (create) mkdir(dir.c_str(), 0700);
    dir += "/w1700k";
    if (create) mkdir(dir.c_str(), 0700);
    return dir + "/fftwf-wisdom";
}

struct timespec wisdom_read = {0, 0};  // the kept plans' modification time when last read; with the planner held

// With the planner held. Gives FFTW the kept plans if they were written since they were last
// read; whether it did.
bool load_wisdom()
{
    const std::string path = wisdom_path(false);
    struct stat st;
    if (path.empty() || stat(path.c_str(), &st)) return false;
    if (st.st_mtim.tv_sec == wisdom_read.tv_sec && st.st_mtim.tv_nsec == wisdom_read.tv_nsec) return false;
    wisdom_read = st.st_mtim;
    return fftwf_import_wisdom_from_filename(path.c_str()) != 0;
}

// With the planner held. All that FFTW knows, which is what was kept and what was measured since.
bool save_wisdom()
{
    const std::string path = wisdom_path(true);
    if (path.empty()) return false;
    const std::string fresh = path + ".new";
    return fftwf_export_wisdom_to_filename(fresh.c_str()) && std::rename(fresh.c_str(), path.c_str()) == 0;
}

// With the planner held: FFTW knows the measured plan of size n.
bool plan_known(unsigned n)
{
    fftwf_complex *in = fftwf_alloc_complex(n);
    const fftwf_plan plan = fftwf_plan_dft_1d(int(n), in, in, FFTW_FORWARD, FFTW_MEASURE | FFTW_WISDOM_ONLY);
    if (plan) fftwf_destroy_plan(plan);
    fftwf_free(in);
    return plan != nullptr;
}

}  // namespace

float spectrum_db(float power) { return 10 * std::log10(power + 1e-20f); }

int spectrum_measure_plan(unsigned n)
{
    SpectrumSettings sized;
    sized.size = n;
    sized.validate();
    const std::string path = wisdom_path(true);
    if (path.empty()) return 1;
    // One process measures at a time, so that each has the machine's caches to itself, and one
    // has each size in hand: several servers started together, as a test run does, each ask for
    // the same size, and all but the first have nothing to do. Both are locks on bytes of one
    // file, which end with the process that holds them: byte 0 while measuring, and a byte of
    // its own for each size (512 is 2^9: bytes 9 to 18) from asking until the plan is kept.
    const int held = open((path + ".lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (held < 0) return 1;
    unsigned power = 0;
    while ((1u << power) < n) ++power;
    const auto lock_byte = [held](off_t at, bool wait) {
        struct flock byte = {};
        byte.l_type = F_WRLCK;
        byte.l_whence = SEEK_SET;
        byte.l_start = at;
        byte.l_len = 1;
        int result;
        while ((result = fcntl(held, wait ? F_SETLKW : F_SETLK, &byte)) < 0 && wait && errno == EINTR) {
        }
        return result == 0;
    };
    if (!lock_byte(power, false)) return 0;
    if (!lock_byte(0, true)) return 1;
    std::lock_guard<std::mutex> lock(planner);
    load_wisdom();
    if (plan_known(n)) return 0;
    fftwf_complex *in = fftwf_alloc_complex(n);
    fftwf_set_timelimit(FFTW_NO_TIMELIMIT);
    const fftwf_plan plan = fftwf_plan_dft_1d(int(n), in, in, FFTW_FORWARD, FFTW_MEASURE);
    if (plan) fftwf_destroy_plan(plan);
    fftwf_free(in);
    if (!plan) return 1;
    load_wisdom();  // what another program kept meanwhile stays kept: FFTW adds a file to what it knows
    return save_wisdom() ? 0 : 1;
}

const std::vector<std::string> &spectrum_windows()
{
    static const std::vector<std::string> names = [] {
        std::vector<std::string> list;
        for (const auto &entry : window_table()) list.push_back(entry.first);
        return list;
    }();
    return names;
}

double spectrum_window_bandwidth(const std::string &window)
{
    const unsigned n = 4096;
    double sum = 0, squares = 0;
    for (const auto &entry : window_table()) {
        if (entry.first != window) continue;
        for (unsigned i = 0; i < n; ++i) {
            double w = 0, sign = 1;
            for (size_t k = 0; k < entry.second.size(); ++k, sign = -sign)
                w += sign * entry.second[k] * std::cos(2 * M_PI * double(k) * i / n);
            sum += w;
            squares += w * w;
        }
    }
    return sum ? n * squares / (sum * sum) : 1;
}

void SpectrumSettings::validate() const
{
    if (size < 512 || size > 262144 || (size & (size - 1)))
        throw std::runtime_error("FFT size must be a power of two from 512 to 262144");
    const auto &names = spectrum_windows();
    if (std::find(names.begin(), names.end(), window) == names.end())
        throw std::runtime_error("unknown FFT window " + window);
    if (!(rate >= 1 && rate <= 100)) throw std::runtime_error("FFT rate must be 1..100 frames per second");
    if (averaging > 100000) throw std::runtime_error("averaging must be at most 100000 blocks");
    if (channel > kBothChannels) throw std::runtime_error("channel must be 0, 1 or 2 (both)");
}

// The plan of one FFT size, in place (a worker's block then stays in 2 MiB at the largest size,
// not 4): the measured one where FFTW knows it, else its estimate, ready at once.
struct SpectrumEngine::Plan {
    fftwf_plan plan;
    bool measured;

    explicit Plan(unsigned n)
    {
        std::lock_guard<std::mutex> lock(planner);
        fftwf_complex *in = fftwf_alloc_complex(n);
        fftwf_set_timelimit(FFTW_NO_TIMELIMIT);
        plan = fftwf_plan_dft_1d(int(n), in, in, FFTW_FORWARD, FFTW_MEASURE | FFTW_WISDOM_ONLY);
        measured = plan != nullptr;
        if (!plan) plan = fftwf_plan_dft_1d(int(n), in, in, FFTW_FORWARD, FFTW_ESTIMATE);
        fftwf_free(in);
    }
    ~Plan()
    {
        std::lock_guard<std::mutex> lock(planner);
        fftwf_destroy_plan(plan);
    }
    Plan(const Plan &) = delete;
    Plan &operator=(const Plan &) = delete;
};

// One FFT size and window, shared by the workers: each runs the plan on its own array.
struct SpectrumEngine::Transform {
    unsigned size;
    std::vector<float> window;
    double scale;              // |X|^2 of a full-scale complex tone -> 1
    std::shared_ptr<const Plan> plan;

    Transform(unsigned n, const std::string &name, std::shared_ptr<const Plan> p)
        : size(n), window(n), plan(std::move(p))
    {
        const std::vector<double> *terms = nullptr;
        for (const auto &entry : window_table())
            if (entry.first == name) terms = &entry.second;
        double sum = 0;
        for (unsigned i = 0; i < n; ++i) {
            double w = 0, sign = 1;
            for (size_t k = 0; k < terms->size(); ++k, sign = -sign)
                w += sign * (*terms)[k] * std::cos(2 * M_PI * double(k) * i / n);
            window[i] = float(w);
            sum += w;
        }
        scale = 1 / ((kFullScale * sum) * (kFullScale * sum));
    }
};

struct SpectrumEngine::Job {
    std::shared_ptr<const StreamView> view;  // the mapping the run lives in
    uint64_t sequence = 0, run = 0, first = 0, stride = 0;
    unsigned blocks = 0;
    std::shared_ptr<const Transform> transform;
    Tuning tuning;
    bool peak = false, remove_dc = false;
    // Sample times in a slot of the run, and where the channel shown begins in a slot: a
    // two-channel run's slots are channel 0's samples, then channel 1's (w1700k_stream.h).
    unsigned per = kSliceSamples, channel = 0;
};

struct SpectrumEngine::Worker {
    std::thread thread;
    unsigned size = 0;
    fftwf_complex *in = nullptr;  // the block, transformed in place
    std::vector<float> power;
    ~Worker() { fftwf_free(in); }
};

SpectrumEngine::SpectrumEngine(const StreamClient &stream, const Tuning &tuning, unsigned workers, Output output)
    : stream_(stream), output_(std::move(output))
{
    latest_.tuning = tuning;
    history_.push_back(latest_);
    stats_.workers = std::max(1u, workers);
    {
        std::lock_guard<std::mutex> lock(planner);
        load_wisdom();
    }
    transform(settings_.size, settings_.window);  // planned now, not while the stream runs past
    refiner_ = std::thread([this] { refine(); });
    for (unsigned i = 0; i < stats_.workers; ++i) workers_.push_back(std::make_unique<Worker>());
    for (auto &w : workers_) w->thread = std::thread([this, p = w.get()] { work(*p); });
    scheduler_ = std::thread([this] { schedule(); });
}

SpectrumEngine::~SpectrumEngine()
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
    }
    work_ready_.notify_all();
    scheduler_.join();
    for (auto &w : workers_) w->thread.join();
    {
        std::lock_guard<std::mutex> lock(transforms_mutex_);
        refine_stop_ = true;
    }
    refine_ready_.notify_all();
    refiner_.join();  // a measurement under way goes on, in its own process: its plan is kept for the next run
}

void SpectrumEngine::configure(const SpectrumSettings &settings)
{
    std::lock_guard<std::mutex> lock(mutex_);
    settings_ = settings;
    ++settings_version_;
}

SpectrumSettings SpectrumEngine::settings() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return settings_;
}

SpectrumEngine::Stats SpectrumEngine::stats() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

namespace {

// Is the segment at (run, from) past slot `index` of run `run_now`?
bool after(uint64_t run, uint64_t from, uint64_t run_now, uint64_t index)
{
    return run != run_now ? run > run_now : from > index;
}

}  // namespace

void SpectrumEngine::add_segment(Segment s)
{
    // Positions only move forward, even when the newest slice has not since the last change.
    if (s.run < latest_.run || (s.run == latest_.run && s.from < latest_.from)) {
        s.run = latest_.run;
        s.from = latest_.from;
    }
    latest_ = s;
    pending_.push_back(s);
    history_.push_back(s);
    while (history_.size() > kHistory) history_.pop_front();
}

void SpectrumEngine::set_tuning(const Tuning &tuning)
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (latest_.valid && latest_.tuning == tuning) return;
    const StreamClient::Position p = stream_.position();
    add_segment({p.run, p.newest + kSettleSlices, true, tuning});
}

void SpectrumEngine::begin_change()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (!latest_.valid) return;
    const StreamClient::Position p = stream_.position();
    add_segment({p.run, p.newest, false, latest_.tuning});
}

Tuning SpectrumEngine::tuning() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return latest_.tuning;
}

std::optional<Tuning> SpectrumEngine::tuning_over(uint64_t run, uint64_t first, uint64_t last) const
{
    std::lock_guard<std::mutex> lock(mutex_);
    const Segment *in_effect = nullptr;
    for (const Segment &s : history_) {
        if (!after(s.run, s.from, run, first)) in_effect = &s;
        else if (!after(s.run, s.from, run, last)) return std::nullopt;  // a change inside the range
    }
    if (!in_effect || !in_effect->valid) return std::nullopt;
    return in_effect->tuning;
}

std::shared_ptr<const SpectrumEngine::Transform> SpectrumEngine::transform(unsigned size, const std::string &window)
{
    std::lock_guard<std::mutex> lock(transforms_mutex_);
    auto &plan = plans_[size];
    if (!plan) {
        plan = std::make_shared<const Plan>(size);
        if (!plan->measured) {
            refine_.push_back(size);
            refine_ready_.notify_all();
        }
    }
    auto &t = transforms_[{size, window}];
    if (!t || t->plan != plan) t = std::make_shared<const Transform>(size, window, plan);
    return t;
}

// Has the plans that were only estimated measured, each size by a process of its own, and has
// the scheduler take each up once it is kept. The workers go on with the estimate meanwhile, and
// nothing here waits for a measurement: not planning another size, not the server's end.
void SpectrumEngine::refine()
{
    std::vector<unsigned> awaited;  // sizes being measured, or queued behind one that is
    for (;;) {
        std::vector<unsigned> fresh;
        {
            std::unique_lock<std::mutex> lock(transforms_mutex_);
            const auto due = [this] { return refine_stop_ || !refine_.empty(); };
            if (awaited.empty()) refine_ready_.wait(lock, due);
            else refine_ready_.wait_for(lock, std::chrono::milliseconds(500), due);
            if (refine_stop_) return;
            for (; !refine_.empty(); refine_.pop_front())
                if (std::find(awaited.begin(), awaited.end(), refine_.front()) == awaited.end())
                    fresh.push_back(refine_.front());
        }
        // From this thread, which runs at the server's own priority (the workers lowered theirs).
        for (unsigned n : fresh)
            if (!wisdom_path(false).empty() && apart::run({"--measure-plan", std::to_string(n)})) awaited.push_back(n);
        std::vector<unsigned> found;
        {
            std::lock_guard<std::mutex> lock(planner);
            if (load_wisdom())
                for (unsigned n : awaited)
                    if (plan_known(n)) found.push_back(n);
        }
        if (found.empty()) continue;
        {
            std::lock_guard<std::mutex> lock(transforms_mutex_);
            for (unsigned n : found) {
                awaited.erase(std::find(awaited.begin(), awaited.end(), n));
                plans_.erase(n);  // the next transform() of this size plans again, from what FFTW now knows
            }
        }
        std::lock_guard<std::mutex> lock(mutex_);
        ++settings_version_;  // the scheduler fetches its transform again
    }
}

// Moves the frontier over the ring (every slot before it holds its slice or an event, or was
// given up as lost) and hands each whole frame behind it to a worker. Frames are periods of
// sample time; their blocks are spread evenly over the period (overlapping when there are more
// than fit). A frame that comes due while every worker is busy and the queue is full is skipped,
// not waited for. Each frame carries the tuning in effect over all of it; one that spans a
// change, or falls in a change in progress, is dropped.
void SpectrumEngine::schedule()
{
    uint64_t run = 0, frontier = 0, next = 0, sequence = 0, applied = 0;  // next: first sample of the next frame
    uint64_t frame_pairs = 1, stride = 0;
    unsigned blocks = 1;
    double share = 1;  // of `blocks`, what the workers keep up with (every-sample mode)
    bool replan = true;
    SpectrumSettings active;
    std::shared_ptr<const Transform> t;
    Segment current;
    std::deque<Segment> upcoming;
    // Sample positions count a run's sample times: `per` of them to a slot, 2048 of a one-channel
    // run and 1024 of a two-channel one, whose frames are of the one channel shown.
    uint64_t per = kSliceSamples;
    unsigned channels = 1;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        current = history_.front();
    }
    for (;;) {
        SpectrumSettings s;
        uint64_t version;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (stopping_) return;
            s = settings_;
            version = settings_version_;
            upcoming.insert(upcoming.end(), pending_.begin(), pending_.end());
            pending_.clear();
        }
        const std::shared_ptr<const StreamView> view = stream_.view();
        const StreamClient::Position position = stream_.position();
        if (!view) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            continue;
        }
        const uint64_t r = position.run, newest = position.newest;
        if (r != run) {
            run = r;
            channels = position.channels == 2 ? 2 : 1;
            per = kSliceSamples / channels;
            frontier = next = 0;
        }
        if (version != applied || !t) {
            t = transform(s.size, s.window);  // also when only its plan was measured since
            active = s;
            applied = version;
            replan = true;
            next = std::max(next, frontier * per);
        }

        uint64_t lost = 0, skipped = 0, dropped = 0, retuned = 0;
        const uint64_t capacity = view->capacity();
        if (newest > frontier + capacity / 2) {  // far behind (planning a new size): what was there is gone
            skipped += newest - capacity / 4 - frontier;
            frontier = newest - capacity / 4;
            next = std::max(next, frontier * per);
        }
        while (frontier < newest) {
            const uint64_t tag = view->tag(frontier);
            if (StreamView::holds_samples(tag, run, frontier) || StreamView::holds_event(tag, run, frontier)) {
                ++frontier;
            } else if (StreamView::holds_loss(tag, run, frontier) || newest > frontier + kHorizon) {
                ++frontier;
                ++lost;
            } else {
                break;
            }
        }
        bool queued = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            for (;;) {
                while (!upcoming.empty() && !after(upcoming.front().run, upcoming.front().from, run, next / per)) {
                    current = upcoming.front();
                    upcoming.pop_front();
                    replan = true;
                }
                if (replan) {
                    const double rate = current.tuning.sample_rate;
                    frame_pairs = std::max<uint64_t>(active.size, uint64_t(std::llround(rate / active.rate)));
                    // "All": enough blocks, overlapping a little, that every sample is in one.
                    blocks = active.averaging ? active.averaging : unsigned((frame_pairs + active.size - 1) / active.size);
                    stride = blocks > 1 ? (frame_pairs - active.size) / (blocks - 1) : 0;
                    replan = false;
                }
                const uint64_t end = next + frame_pairs;
                if (end > frontier * per) break;
                if (!upcoming.empty() &&
                    !after(upcoming.front().run, upcoming.front().from, run, (end - 1) / per)) {
                    ++retuned;  // spans a change: resume where it takes effect
                    next = upcoming.front().from * per;
                    continue;
                }
                if (!current.valid) {
                    ++retuned;
                } else if (jobs_.size() < 2 * workers_.size()) {
                    // Every sample, as far as the workers manage: fewer blocks, spread evenly.
                    unsigned use = blocks;
                    if (!active.averaging && blocks > 1) {
                        if (jobs_.empty()) share = std::min(1.0, share + kShareUp);
                        else if (jobs_.size() >= workers_.size()) share = std::max(1.0 / blocks, share * kShareEase);
                        use = std::clamp(unsigned(std::lround(blocks * share)), 1u, blocks);
                    }
                    // One job a channel shown, over the same samples: with both, channel 0's
                    // frame and then channel 1's, which go out in that order (finish()).
                    const bool both = channels == 2 && active.channel == SpectrumSettings::kBothChannels;
                    const unsigned first_channel = both || channels != 2 ? 0 : std::min(active.channel, 1u);
                    for (unsigned c = first_channel; c <= (both ? 1u : first_channel); ++c) {
                        Job job;
                        job.view = view;
                        job.sequence = sequence++;
                        job.run = run;
                        job.first = next;
                        job.stride = use == blocks ? stride : use > 1 ? (frame_pairs - active.size) / (use - 1) : 0;
                        job.blocks = use;
                        job.transform = t;
                        job.tuning = current.tuning;
                        job.peak = active.peak;
                        job.remove_dc = active.remove_dc;
                        job.per = unsigned(per);
                        job.channel = c;
                        jobs_.push_back(std::move(job));
                    }
                    queued = true;
                } else {
                    dropped += frame_pairs;
                    if (!active.averaging) share = std::max(1.0 / blocks, share * kShareDown);
                }
                next = end;
            }
            stats_.share = active.averaging ? 1 : share;
            stats_.measured = t->plan->measured;
            stats_.slices_lost += lost;
            stats_.slices_skipped += skipped;
            stats_.pairs_dropped += dropped;
            stats_.frames_retuned += retuned;
            stats_.backlog = newest - frontier;
            stats_.queued = unsigned(jobs_.size());
        }
        if (queued) work_ready_.notify_all();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

void SpectrumEngine::work(Worker &w)
{
    // This thread alone (on Linux a nice level is per thread); refused: it runs as the others do.
    setpriority(PRIO_PROCESS, id_t(syscall(SYS_gettid)), kWorkerNice);
    for (;;) {
        Job job;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            work_ready_.wait(lock, [this] { return stopping_ || !jobs_.empty(); });
            if (stopping_) return;
            job = std::move(jobs_.front());
            jobs_.pop_front();
        }
        const Transform &t = *job.transform;
        const unsigned n = t.size;
        if (w.size != n) {
            fftwf_free(w.in);
            w.in = fftwf_alloc_complex(n);
            w.power.assign(n, 0.0f);
            w.size = n;
        }
        std::fill(w.power.begin(), w.power.end(), 0.0f);

        unsigned used = 0;
        uint64_t missing = 0;
        for (unsigned b = 0; b < job.blocks; ++b) {
            const uint64_t position = job.first + b * job.stride;
            const uint64_t s0 = position / job.per, s1 = (position + n - 1) / job.per;
            auto present = [&] {
                for (uint64_t s = s0; s <= s1; ++s)
                    if (!StreamView::holds_samples(job.view->tag(s), job.run, s)) return false;
                return true;
            };
            if (!present()) {
                missing += n;
                continue;
            }
            // The block's samples, slice by slice: (fn)(sample number in block, I, Q).
            auto each = [&](auto fn) {
                uint64_t p = position;
                for (unsigned k = 0; k < n;) {
                    const int16_t *iq = job.view->slice(p / job.per) + 2 * size_t(job.channel) * job.per;
                    unsigned offset = unsigned(p % job.per), m = std::min(n - k, job.per - offset);
                    for (unsigned j = 0; j < m; ++j) fn(k + j, float(iq[2 * (offset + j)]), float(iq[2 * (offset + j) + 1]));
                    k += m;
                    p += m;
                }
            };
            float mean_i = 0, mean_q = 0;
            if (job.remove_dc) {
                double sum_i = 0, sum_q = 0;
                each([&](unsigned, float i, float q) {
                    sum_i += i;
                    sum_q += q;
                });
                mean_i = float(sum_i / n);
                mean_q = float(sum_q / n);
            }
            const float *window = t.window.data();
            fftwf_complex *in = w.in;
            each([&](unsigned k, float i, float q) {
                in[k][0] = (i - mean_i) * window[k];
                in[k][1] = (q - mean_q) * window[k];
            });
            std::atomic_thread_fence(std::memory_order_acquire);
            if (!present()) {  // a slot was taken over while we read it
                missing += n;
                continue;
            }
            fftwf_execute_dft(t.plan->plan, w.in, w.in);
            const fftwf_complex *out = w.in;
            float *power = w.power.data();
            if (job.peak) {
                for (unsigned k = 0; k < n; ++k)
                    power[k] = std::max(power[k], out[k][0] * out[k][0] + out[k][1] * out[k][1]);
            } else {
                for (unsigned k = 0; k < n; ++k) power[k] += out[k][0] * out[k][0] + out[k][1] * out[k][1];
            }
            ++used;
        }

        std::shared_ptr<SpectrumFrame> frame;
        if (used) {
            frame = std::make_shared<SpectrumFrame>();
            frame->run = job.run;
            frame->index = job.first;
            frame->center_hz = job.tuning.center_hz;
            frame->sample_rate = job.tuning.sample_rate;
            frame->blocks = used;
            frame->peak = job.peak;
            frame->channel = job.channel;
            frame->power.resize(n);
            // I + jQ is RF minus LO (a carrier below the LO has a negative frequency): display
            // bin i is RF offset (i - n/2) * rate / n, FFT bin (i - n/2) mod n: the two halves
            // change places.
            const float scale = float(t.scale / (job.peak ? 1 : used));
            const float *power = w.power.data();
            float *shown = frame->power.data();
            for (unsigned i = 0; i < n / 2; ++i) shown[i] = power[i + n / 2] * scale;
            for (unsigned i = 0; i < n / 2; ++i) shown[i + n / 2] = power[i] * scale;
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stats_.blocks += used;
            stats_.pairs_used += uint64_t(used) * n;
            stats_.pairs_missing += missing;
            if (frame) ++stats_.frames;
        }
        finish(job.sequence, std::move(frame));
    }
}

// Results go out in the order the frames were scheduled; a frame with no usable block leaves
// no gap.
void SpectrumEngine::finish(uint64_t sequence, std::shared_ptr<SpectrumFrame> frame)
{
    std::lock_guard<std::mutex> lock(results_mutex_);
    results_[sequence] = std::move(frame);
    for (auto it = results_.find(emit_next_); it != results_.end(); it = results_.find(emit_next_)) {
        std::shared_ptr<SpectrumFrame> f = std::move(it->second);
        results_.erase(it);
        ++emit_next_;
        if (f) output_(std::move(f));
    }
}
