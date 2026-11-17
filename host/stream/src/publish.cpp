// SPDX-License-Identifier: MIT
#include "publish.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <stdexcept>

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <unistd.h>

#include "convert.h"

namespace {

constexpr unsigned kChunk = 16;            // slots per job
constexpr uint64_t kHorizon = 1024;        // a slot this far behind the newest completion received is overdue
// An overdue slot is given this long to arrive before it counts as lost: on a busy host a receive
// thread can be kept off its CPU for tens of milliseconds while the others run on, and what it
// then delivers is late, not lost; and the two halves of a split stream leave the unit by different
// paths (at 320 Msps on a 5 Gbit/s link each was seen 0.035 s behind at worst over five minutes,
// and 0.14 s and 0.21 s while the unit's senders still shared their CPUs). A slot is given up at
// once when the newest completion is half the receive ring ahead of it, before its place there is
// written again.
constexpr uint64_t kLateNs = 250000000;
constexpr uint64_t kIdleNs = 5000000;      // nothing new for this long: publish what has arrived
constexpr uint64_t kReportNs = 100000000;  // heartbeat and counters

template <class T> void store(T *p, T v) { __atomic_store_n(p, v, __ATOMIC_RELEASE); }
template <class T> T load(const T *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }

}  // namespace

// ---- the shared ring ------------------------------------------------------------------------------

SharedRing::SharedRing(const std::string &name, unsigned slices) : name_(name)
{
    if (name.size() < 2 || name[0] != '/' || name.find('/', 1) != std::string::npos)
        throw std::runtime_error("the shared-memory name must look like /w1700k-stream, not " + name);
    if (!slices || (slices & (slices - 1))) throw std::runtime_error("the ring must hold a power of two slices");
    size_t old_bytes = 0;
    if (w1700k_stream_header *old = w1700k_stream_open(name.c_str(), 0, &old_bytes, nullptr)) {
        const uint64_t pid = old->service_pid;
        const bool alive = w1700k_stream_alive(old) && kill(pid_t(pid), 0) == 0;
        munmap(old, old_bytes);
        if (alive) throw std::runtime_error("another w1700k-stream (pid " + std::to_string(pid) + ") serves " + name);
    }
    shm_unlink(name.c_str());  // a stopped service's object: consumers still mapping it keep their copy

    const uint64_t tags = W1700K_STREAM_HEADER_BYTES;
    const uint64_t samples = tags + ((uint64_t(slices) * 8 + 4095) & ~uint64_t(4095));
    bytes_ = size_t(samples + uint64_t(slices) * W1700K_STREAM_SLICE * 4);
    const int fd = shm_open(name.c_str(), O_CREAT | O_EXCL | O_RDWR, 0600);
    if (fd < 0 && errno == EEXIST) {
        // Still there after the unlink above: not ours to remove. In /dev/shm, which is sticky,
        // that is an object another user made under the name. Its consumers would refuse it
        // (w1700k_stream_open); the service says why it cannot start instead.
        struct stat st;
        const bool known = stat(("/dev/shm" + name).c_str(), &st) == 0;
        throw std::runtime_error(name + " exists and is not this user's to replace" +
                                 (known && st.st_uid != geteuid() ? " (it belongs to user " + std::to_string(st.st_uid) + ")" : "") +
                                 ": have it removed (/dev/shm" + name + "), or give the service and its consumers "
                                 "another name (--name)");
    }
    if (fd < 0) throw std::runtime_error("shm_open " + name + ": " + std::strerror(errno));
    if (ftruncate(fd, off_t(bytes_))) {
        const std::string why = std::strerror(errno);
        close(fd);
        shm_unlink(name.c_str());
        throw std::runtime_error("sizing " + name + ": " + why);
    }
    void *p = mmap(nullptr, bytes_, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        shm_unlink(name.c_str());
        throw std::runtime_error("mapping " + name + ": " + std::strerror(errno));
    }
    header_ = static_cast<w1700k_stream_header *>(p);
    header_->version = W1700K_STREAM_VERSION;
    header_->header_bytes = W1700K_STREAM_HEADER_BYTES;
    header_->slice_samples = W1700K_STREAM_SLICE;
    header_->ring_slices = slices;
    header_->tags_offset = tags;
    header_->samples_offset = samples;
    header_->total_bytes = bytes_;
    header_->service_pid = uint64_t(getpid());
    store(&header_->heartbeat_ns, w1700k_stream_now_ns());
}

void SharedRing::ready()
{
    store(&header_->heartbeat_ns, w1700k_stream_now_ns());
    store(&header_->magic, W1700K_STREAM_MAGIC);  // last: a consumer that sees it sees the layout
}

SharedRing::~SharedRing()
{
    if (!header_) return;
    store(&header_->heartbeat_ns, uint64_t(0));  // consumers still mapping it see the service gone
    shm_unlink(name_.c_str());
    munmap(header_, bytes_);
}

// ---- the publisher --------------------------------------------------------------------------------

// A run of a correction job's input samples: `count` sample words, or as many zeros.
struct Span {
    const uint32_t *words;
    unsigned count;
};

struct Publisher::Worker {
    std::thread thread;
    FlattenRun flatten;
    std::vector<Slot> kinds;
    std::vector<Span> spans;         // a correction job's input samples, in order
    std::vector<int16_t *> targets;  // ... and where each of its sample slots' output goes (nullptr: lost)
};

Publisher::Publisher(const SampleRing &raw, Ingest &ingest, w1700k_stream_header *out, unsigned workers)
    : raw_(raw), ingest_(ingest), out_(out), worker_count_(std::max(1u, workers))
{
}

void Publisher::start()
{
    if (scheduler_.joinable()) return;
    for (unsigned i = 0; i < worker_count_; ++i) workers_.push_back(std::make_unique<Worker>());
    for (auto &w : workers_) w->thread = std::thread([this, p = w.get()] { work(*p); });
    scheduler_ = std::thread([this] { schedule(); });
}

Publisher::~Publisher()
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
    }
    work_ready_.notify_all();
    job_done_.notify_all();
    if (scheduler_.joinable()) scheduler_.join();
    for (auto &w : workers_)
        if (w->thread.joinable()) w->thread.join();
}

const Flatten *Publisher::correction() const
{
    if (!load(&out_->flatten)) return nullptr;
    const uint64_t rate = load(&out_->radio_sample_rate);
    if (!rate || load(&out_->radio_capture_rate) != rate || load(&out_->radio_filter_hz) != rate) return nullptr;
    return Flatten::for_rate(double(rate));
}

void Publisher::publish_position(uint64_t run, uint64_t newest)
{
    const uint64_t seq = load(&out_->position_seq);
    store(&out_->position_seq, seq + 1);
    std::atomic_thread_fence(std::memory_order_release);
    store(&out_->run, run);
    store(&out_->newest, newest);
    store(&out_->position_seq, seq + 2);
}

void Publisher::report()
{
    const Ingest::Stats s = ingest_.stats();
    store(&out_->datagrams, s.datagrams);
    store(&out_->bytes, s.bytes);
    store(&out_->slices, s.slices);
    store(&out_->packed, s.packed);
    store(&out_->reduced, s.reduced);
    store(&out_->suspect, s.suspect);
    store(&out_->events, s.events);
    store(&out_->pieces, s.pieces);
    store(&out_->bad, s.bad);
    store(&out_->late, s.late);
    store(&out_->before_start, s.before_start);
    store(&out_->duplicates, s.duplicates);
    store(&out_->socket_drops, s.socket_drops);
    store(&out_->spare_words, s.spare_words);
    store(&out_->stale_slices, s.stale_slices);
    store(&out_->stale_lines, s.stale_lines);
    store(&out_->lost, lost_.load());
    store(&out_->overruns, overruns_.load());
    store(&out_->flattened, flattened_.load());
    store(&out_->saturated, saturated_.load());
    store(&out_->received, s.newest);
    store(&out_->run_started_ns, uint64_t(s.run_started * 1e9));
    store(&out_->last_any_ns, uint64_t(s.last_any * 1e9));
    store(&out_->heartbeat_ns, w1700k_stream_now_ns());
}

// One thread walks the receive ring in order: a slot is resolved when its completion (samples or
// an event) is in, or when it is given up as lost: kHorizon newer ones have been in for kLateNs
// (one wait covers every slot that was overdue when it began), or half the ring is newer.
// Whole chunks of resolved
// slots go to the workers, with one more resolved slot after them when the correction needs
// samples beyond the chunk; an idle stream's tail goes as it is. Finished chunks are published in
// order.
void Publisher::schedule()
{
    uint64_t run = 0, resolved = 0, dispatched = 0, published = 0, sequence = 0, next_publish = 0;
    uint64_t seen_newest = 0, seen_at = 0, reported = 0, restarts = load(&out_->restart_requests);
    uint64_t late_since = 0, late_newest = 0;  // the wait for overdue slots: its start, and the newest then
    const Flatten *active = nullptr;
    for (;;) {
        const uint64_t t = w1700k_stream_now_ns();
        if (t - reported >= kReportNs) {
            report();
            reported = t;
        }
        const uint64_t requests = load(&out_->restart_requests);
        if (requests != restarts) {
            restarts = requests;
            ingest_.restart_run();
        }

        const Ingest::Position p = ingest_.position();
        if (p.run != run) {
            // A new stream: the old one's chunks finish and are published, then it starts over.
            std::unique_lock<std::mutex> lock(mutex_);
            job_done_.wait(lock, [&] { return stopping_ || (jobs_.empty() && !busy_); });
            if (stopping_) return;
            for (auto it = done_.find(next_publish); it != done_.end(); it = done_.find(next_publish)) {
                published = it->second.last;
                done_.erase(it);
                ++next_publish;
            }
            lock.unlock();
            if (run) publish_position(run, published);
            run = p.run;
            resolved = dispatched = published = 0;
            seen_newest = 0;
            late_since = 0;
            publish_position(run, 0);
        }
        if (p.newest != seen_newest) {
            seen_newest = p.newest;
            seen_at = t;
        }
        const bool idle = t - seen_at > kIdleNs;

        bool overdue = false;
        while (resolved < p.newest) {
            const uint64_t tag = raw_.tag(resolved);
            if (tag == SampleRing::tag_of(run, resolved) || tag == SampleRing::event_tag_of(run, resolved)) {
                ++resolved;
                continue;
            }
            if (p.newest <= resolved + kHorizon) break;
            if (p.newest > resolved + raw_.capacity_slices() / 2) {  // too far behind to wait for
                ++resolved;
                continue;
            }
            overdue = true;
            if (!late_since) {
                late_since = t;
                late_newest = p.newest;
            }
            if (t - late_since < kLateNs) break;
            if (late_newest > resolved + kHorizon) {  // overdue for the whole wait: lost
                ++resolved;
                continue;
            }
            late_since = t;  // it fell overdue during the last wait: its own begins
            late_newest = p.newest;
            break;
        }
        if (!overdue) late_since = 0;

        const Flatten *f = correction();
        if (f != active) {  // what applies, as soon as it changes
            active = f;
            store(&out_->flatten_active, uint64_t(f != nullptr));
            store(&out_->flatten_rate, f ? uint64_t(f->rate()) : uint64_t(0));
        }
        bool waiting = true;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            if (stopping_) return;
            while (jobs_.size() + busy_ < 2 * worker_count_) {
                Job job{sequence, run, dispatched, 0, f, false};
                if (resolved >= dispatched + kChunk + (f ? 1 : 0)) {
                    job.last = dispatched + kChunk;
                } else if (idle && resolved > dispatched) {
                    job.last = resolved;
                    job.flush = true;
                } else {
                    break;
                }
                jobs_.push_back(job);
                ++sequence;
                dispatched = job.last;
                waiting = false;
                work_ready_.notify_one();
            }
            bool moved = false;
            for (auto it = done_.find(next_publish); it != done_.end(); it = done_.find(next_publish)) {
                published = it->second.last;
                done_.erase(it);
                ++next_publish;
                moved = true;
            }
            if (moved) {
                lock.unlock();
                publish_position(run, published);
                continue;
            }
            if (waiting) job_done_.wait_for(lock, std::chrono::microseconds(200));
        }
    }
}

void Publisher::work(Worker &w)
{
    for (;;) {
        Job job;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            work_ready_.wait(lock, [&] { return stopping_ || !jobs_.empty(); });
            if (stopping_) return;
            job = jobs_.front();
            jobs_.pop_front();
            ++busy_;
        }
        process(w, job);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            --busy_;
            done_[job.sequence] = job;
        }
        job_done_.notify_all();
    }
}

Publisher::Slot Publisher::classify(uint64_t run, uint64_t index, bool) const
{
    const uint64_t tag = raw_.tag(index);
    if (tag == SampleRing::tag_of(run, index)) return Slot::Data;
    if (tag == SampleRing::event_tag_of(run, index)) return Slot::Event;
    return Slot::Lost;
}

// A lost slot whose place in the receive ring holds a later completion of the run: it may have
// arrived and been overwritten before it was published.
bool Publisher::overrun(uint64_t run, uint64_t index) const
{
    const uint64_t tag = raw_.tag(index) & ~(uint64_t(1) << 63);
    return (tag >> 40) == run && (tag & ((uint64_t(1) << 40) - 1)) > index + 1;
}

void Publisher::process(Worker &w, const Job &job)
{
    uint64_t *tags = w1700k_stream_tags(out_);
    const uint64_t mask = out_->ring_slices - 1;
    auto claim = [&](uint64_t index) {
        __atomic_store_n(&tags[index & mask], uint64_t(0), __ATOMIC_RELAXED);
        std::atomic_thread_fence(std::memory_order_release);
    };
    auto finish = [&](uint64_t index, uint64_t kind) {
        __atomic_store_n(&tags[index & mask], w1700k_stream_tag(job.run, index) | kind, __ATOMIC_RELEASE);
    };

    w.kinds.resize(job.last - job.first);
    for (uint64_t s = job.first; s < job.last; ++s) w.kinds[s - job.first] = classify(job.run, s, job.flush);

    if (!job.flatten) {
        uint64_t lost = 0, overruns = 0;
        for (uint64_t s = job.first; s < job.last; ++s) {
            const Slot kind = w.kinds[s - job.first];
            if (kind == Slot::Event) {
                finish(s, W1700K_STREAM_TAG_EVENT);
                continue;
            }
            if (kind == Slot::Data) {
                claim(s);
                words_to_int16(raw_.slice(s), kSliceSamples, w1700k_stream_slice(out_, s));
                if (raw_.tag(s) == SampleRing::tag_of(job.run, s)) {
                    finish(s, 0);
                    continue;
                }
            }
            finish(s, W1700K_STREAM_TAG_LOST);  // never arrived, or overwritten while being copied
            ++lost;
            if (overrun(job.run, s)) ++overruns;
        }
        if (lost) lost_ += lost;
        if (overruns) overruns_ += overruns;
        return;
    }

    // The correction: the chunk's samples with kDelay before and after it. Events take no sample
    // time (skipped); lost completions and anything before the run or not yet arrived are zeros.
    // The samples are never laid out as one array: each transform block is filled from the receive
    // ring and its outputs rounded straight into the shared ring, so a worker's working set is the
    // block (16 KiB), not the job's samples twice over as floats.
    const unsigned D = Flatten::kDelay;
    auto words_of = [&](uint64_t s, Slot kind) { return kind == Slot::Data ? raw_.slice(s) : nullptr; };
    w.spans.clear();
    w.targets.clear();
    {
        // before: walk back over the slots that hold samples (an event holds none)
        unsigned need = D;
        const size_t at = w.spans.size();
        for (uint64_t s = job.first; need && s-- > 0;) {
            const Slot kind = classify(job.run, s, false);
            if (kind == Slot::Event) continue;
            const unsigned take = std::min(need, kSliceSamples);
            need -= take;
            const uint32_t *words = words_of(s, kind);
            w.spans.push_back({words ? words + (kSliceSamples - take) : nullptr, take});
        }
        if (need) w.spans.push_back({nullptr, need});  // before the run's first slot
        std::reverse(w.spans.begin() + at, w.spans.end());
    }
    size_t n = 0;
    for (uint64_t s = job.first; s < job.last; ++s) {
        const Slot kind = w.kinds[s - job.first];
        if (kind == Slot::Event) continue;
        w.spans.push_back({words_of(s, kind), kSliceSamples});
        if (kind == Slot::Data) claim(s);
        w.targets.push_back(kind == Slot::Data ? w1700k_stream_slice(out_, s) : nullptr);
        n += kSliceSamples;
    }
    {
        unsigned need = D;
        for (uint64_t s = job.last; need && s < job.last + 4; ++s) {
            const Slot kind = classify(job.run, s, false);
            if (kind == Slot::Event) continue;
            const unsigned take = std::min(need, kSliceSamples);
            w.spans.push_back({words_of(s, kind), take});
            need -= take;
        }
        if (need) w.spans.push_back({nullptr, need});  // not in yet
    }

    // Overlap-save (flatten.h): block j takes inputs [j, j + kFft) of the n + 2 kDelay laid out
    // above and gives outputs [j, j + kBlock), output t centred on input t + kDelay.
    uint64_t clipped = 0;
    size_t span = 0, offset = 0;  // where input j is
    fftwf_complex *const block = w.flatten.block();
    for (size_t j = 0; j < n; j += Flatten::kBlock) {
        size_t filled = 0, sp = span, off = offset;
        while (filled < Flatten::kFft && sp < w.spans.size()) {
            const Span &a = w.spans[sp];
            const unsigned take = unsigned(std::min<size_t>(Flatten::kFft - filled, a.count - off));
            if (a.words) words_to_float(a.words + off, take, block[filled]);
            else std::memset(block[filled], 0, take * sizeof(fftwf_complex));
            filled += take;
            off += take;
            if (off == a.count) {
                ++sp;
                off = 0;
            }
        }
        if (filled < Flatten::kFft) std::memset(block[filled], 0, (Flatten::kFft - filled) * sizeof(fftwf_complex));
        for (size_t step = Flatten::kBlock; step && span < w.spans.size();) {  // input j + kBlock, for the next block
            const size_t take = std::min<size_t>(step, w.spans[span].count - offset);
            step -= take;
            offset += take;
            if (offset == w.spans[span].count) {
                ++span;
                offset = 0;
            }
        }
        w.flatten.filter(*job.flatten);
        const float *y = block[Flatten::kTaps - 1];
        for (size_t at = j, left = std::min<size_t>(Flatten::kBlock, n - j); left;) {
            const size_t slot = at / kSliceSamples, o = at % kSliceSamples, take = std::min<size_t>(left, kSliceSamples - o);
            if (int16_t *dst = w.targets[slot]) clipped += floats_to_int16(y, unsigned(2 * take), dst + 2 * o);
            y += 2 * take;
            at += take;
            left -= take;
        }
    }

    uint64_t lost = 0, overruns = 0, flat = 0;
    for (uint64_t s = job.first; s < job.last; ++s) {
        const Slot kind = w.kinds[s - job.first];
        if (kind == Slot::Event) {
            finish(s, W1700K_STREAM_TAG_EVENT);
        } else if (kind == Slot::Data && raw_.tag(s) == SampleRing::tag_of(job.run, s)) {
            finish(s, W1700K_STREAM_TAG_FLAT);
            ++flat;
        } else {
            finish(s, W1700K_STREAM_TAG_LOST);  // never arrived, or overwritten while it was read
            ++lost;
            if (overrun(job.run, s)) ++overruns;
        }
    }
    if (lost) lost_ += lost;
    if (overruns) overruns_ += overruns;
    if (flat) flattened_ += flat;
    if (clipped) saturated_ += clipped;
}
