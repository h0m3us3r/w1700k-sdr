// SPDX-License-Identifier: MIT
#include "ring.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <thread>

#include <sys/mman.h>
#include <sys/stat.h>

namespace {

template <class T> T load(const T *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }
template <class T> void store(T *p, T v) { __atomic_store_n(p, v, __ATOMIC_RELEASE); }

constexpr unsigned kSlice = W1700K_STREAM_SLICE;
constexpr float kToFloat = 1.0f / W1700K_STREAM_FULL_SCALE;

uint64_t tag_at(const w1700k_stream_header *h, uint64_t index)
{
    return load(&w1700k_stream_tags(h)[index & (h->ring_slices - 1)]);
}

}  // namespace

RingView::~RingView() { munmap(h_, bytes_); }

Ring::Ring(std::string name) : name_(std::move(name)) {}

std::shared_ptr<const RingView> Ring::view()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (view_) {
        // The same object while it is live; a stopped service's is replaced once a new one
        // exists under the name.
        if (view_->alive()) return view_;
        struct stat st;
        const int fd = shm_open(name_.c_str(), O_RDONLY, 0);
        if (fd < 0) return view_;
        const bool same = fstat(fd, &st) == 0 && uint64_t(st.st_ino) == view_->inode();
        close(fd);
        if (same) return view_;
    }
    size_t bytes = 0;
    uint64_t inode = 0;
    w1700k_stream_header *h = w1700k_stream_open(name_.c_str(), 1, &bytes, &inode);
    if (!h) return view_;
    // Every page mapped now. Left to the first read of each, a reader of a 320 Msps stream takes
    // a page fault per 4 KiB all through its first lap of the ring, and on a small host falls a
    // whole ring behind there (seen: one gap of 0.15 s at the start of every stream).
    bool mapped = false;
#ifdef MADV_POPULATE_READ
    mapped = madvise(h, bytes, MADV_POPULATE_READ) == 0;
#endif
    if (!mapped) {
        volatile unsigned char touched = 0;
        for (size_t at = 0; at < bytes; at += 4096) touched = touched + reinterpret_cast<const unsigned char *>(h)[at];
    }
    view_ = std::make_shared<const RingView>(h, bytes, inode);
    write_control(h);
    return view_;
}

bool Ring::alive()
{
    const auto v = view();
    return v && v->alive();
}

void Ring::write_control(w1700k_stream_header *h)
{
    if (radio_known_) {
        store(&h->radio_sample_rate, sample_rate_);
        store(&h->radio_capture_rate, capture_rate_);
        store(&h->radio_filter_hz, filter_hz_);
    }
    if (flatten_set_) store(&h->flatten, uint64_t(flatten_));
}

void Ring::set_radio(double sample_rate, double capture_rate, double filter_hz)
{
    std::lock_guard<std::mutex> lock(mutex_);
    sample_rate_ = uint64_t(std::llround(sample_rate));
    capture_rate_ = uint64_t(std::llround(capture_rate));
    filter_hz_ = uint64_t(std::llround(filter_hz));
    radio_known_ = true;
    if (view_) write_control(view_->header());
}

void Ring::set_flatten(bool on)
{
    std::lock_guard<std::mutex> lock(mutex_);
    flatten_ = on;
    flatten_set_ = true;
    if (view_) write_control(view_->header());
}

long long Reader::now_ns() const { return (long long)w1700k_stream_now_ns(); }

void Reader::start(double rate)
{
    view_ = ring_.view();
    uint64_t run = 0, newest = 0;
    if (view_ && view_->alive()) w1700k_stream_position(view_->header(), &run, &newest);
    // A run in progress is read from now on, timed from now; with none, the next one from its
    // first slice.
    joined_ = run != 0;
    run_ = run;
    index_ = newest;
    offset_ = 0;
    rebase(run ? now_ns() : 0, rate);
    tally_ = Tally();
}

void Reader::rebase(long long at_ns, double rate)
{
    base_ns_ = at_ns;
    samples_ = 0;
    rate_ = rate;
}

void Reader::skip(uint64_t slices)
{
    samples_ += slices * kSlice - offset_;
    index_ += slices;
    offset_ = 0;
    tally_.gaps++;
}

long Reader::read(int16_t *cs16, float *cf32, size_t count, long timeout_us, double rate, long long &time_ns)
{
    const auto next_ns = [this] {
        return base_ns_ + (rate_ > 0 ? std::llround(double(samples_) * 1e9 / rate_) : 0LL);
    };
    const long long deadline = now_ns() + (long long)timeout_us * 1000;
    // The radio's rate changed: the samples from here on are at the new one.
    if (rate > 0 && rate != rate_) rebase(next_ns(), rate);
    size_t got = 0;

    for (;;) {
        const auto current = ring_.view();
        if (current != view_) {  // another service: its runs are new ones
            if (got) break;
            view_ = current;
            joined_ = false;
        }
        uint64_t run = 0, newest = 0;
        if (view_ && view_->alive()) w1700k_stream_position(view_->header(), &run, &newest);
        if (run) {
            const w1700k_stream_header *h = view_->header();
            const uint64_t ring = h->ring_slices;
            if (!joined_ || run != run_) {
                // A new run: from its first slice, or where that is no longer in the ring, from
                // now; timed from when the service saw it begin.
                if (got) break;
                run_ = run;
                index_ = newest > ring / 2 ? newest : 0;
                offset_ = 0;
                joined_ = true;
                rebase(std::max(next_ns(), (long long)load(&h->run_started_ns)), rate_);
            }
            if (index_ < newest) {
                tally_.behind = std::max(tally_.behind, newest - index_);
                const uint64_t want = w1700k_stream_tag(run_, index_);
                const uint64_t tag = tag_at(h, index_);
                bool lapped = newest - index_ > ring - ring / 8;  // what is next is being overwritten
                if (!lapped && tag == (want | W1700K_STREAM_TAG_EVENT)) {
                    index_++;  // no samples and no sample time
                    continue;
                }
                if (!lapped && tag == (want | W1700K_STREAM_TAG_LOST)) {
                    if (got) break;
                    uint64_t n = 1;
                    while (index_ + n < newest &&
                           tag_at(h, index_ + n) == (w1700k_stream_tag(run_, index_ + n) | W1700K_STREAM_TAG_LOST))
                        n++;
                    skip(n);
                    time_ns = next_ns();
                    return kGap;
                }
                if (!lapped && (tag & ~W1700K_STREAM_TAG_FLAT) == want) {
                    const size_t n = std::min<size_t>(count - got, kSlice - offset_);
                    const int16_t *src = w1700k_stream_slice(h, index_) + 2 * offset_;
                    if (cs16) {
                        std::memcpy(cs16 + 2 * got, src, n * 2 * sizeof(int16_t));
                    } else {
                        float *dst = cf32 + 2 * got;
                        for (size_t k = 0; k < 2 * n; k++) dst[k] = float(src[k]) * kToFloat;
                    }
                    __atomic_thread_fence(__ATOMIC_ACQUIRE);
                    if (tag_at(h, index_) == tag) {
                        got += n;
                        offset_ += unsigned(n);
                        if (offset_ == kSlice) {
                            offset_ = 0;
                            index_++;
                        }
                        if (got == count) break;
                        continue;
                    }
                    lapped = true;  // overwritten while it was copied: those samples are not given
                }
                // Lapped, or the slot holds something else: the run may have changed meanwhile.
                uint64_t run_now = 0, newest_now = 0;
                w1700k_stream_position(h, &run_now, &newest_now);
                if (run_now != run_) continue;
                if (got) break;
                tally_.lapped += lapped;
                skip(lapped && newest_now > index_ + ring / 2 ? newest_now - ring / 2 - index_ : 1);
                time_ns = next_ns();
                return kGap;
            }
        }
        if (got) break;
        if (now_ns() >= deadline) return kTimeout;
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    time_ns = next_ns();
    samples_ += got;
    tally_.samples += got;
    return long(got);
}
