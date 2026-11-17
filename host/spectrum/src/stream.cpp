// SPDX-License-Identifier: MIT
#include "stream.h"

#include <cmath>
#include <sys/mman.h>
#include <sys/stat.h>

namespace {

template <class T> T load(const T *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }
template <class T> void store(T *p, T v) { __atomic_store_n(p, v, __ATOMIC_RELEASE); }

}  // namespace

StreamView::~StreamView() { munmap(h_, bytes_); }

StreamClient::StreamClient(std::string name) : name_(std::move(name)) { refresh(); }

void StreamClient::refresh()
{
    std::shared_ptr<const StreamView> current = view();
    if (current) {
        // The same object while it is live; a stopped service's object is replaced once a new one
        // exists under the name.
        if (w1700k_stream_alive(current->header())) return;
        struct stat st;
        const int fd = shm_open(name_.c_str(), O_RDONLY, 0);
        if (fd < 0) return;
        const bool same = fstat(fd, &st) == 0 && uint64_t(st.st_ino) == current->inode();
        close(fd);
        if (same) return;
    }
    size_t bytes = 0;
    uint64_t inode = 0;
    w1700k_stream_header *h = w1700k_stream_open(name_.c_str(), 1, &bytes, &inode);
    if (!h) return;
    std::lock_guard<std::mutex> lock(mutex_);
    view_ = std::make_shared<const StreamView>(h, bytes, inode, ++epoch_);
    write_control(h);
}

std::shared_ptr<const StreamView> StreamClient::view() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return view_;
}

bool StreamClient::alive() const
{
    const auto v = view();
    return v && w1700k_stream_alive(v->header());
}

StreamClient::Position StreamClient::position() const
{
    const auto v = view();
    if (!v) return {};
    uint64_t run, newest;
    w1700k_stream_position(v->header(), &run, &newest);
    return {run ? v->run_id(run) : 0, newest};
}

int StreamClient::receive_cpu() const
{
    const auto v = view();
    return v ? int(load(&v->header()->receive_cpu)) - 1 : -1;
}

StreamClient::Stats StreamClient::stats() const
{
    Stats s;
    const auto v = view();
    if (!v) return s;
    const w1700k_stream_header *h = v->header();
    s.run = load(&h->run);
    s.ring_slices = h->ring_slices;
    s.datagrams = load(&h->datagrams);
    s.bytes = load(&h->bytes);
    s.slices = load(&h->slices);
    s.packed = load(&h->packed);
    s.events = load(&h->events);
    s.lost = load(&h->lost);
    s.pieces = load(&h->pieces);
    s.bad = load(&h->bad);
    s.late = load(&h->late);
    s.before_start = load(&h->before_start);
    s.duplicates = load(&h->duplicates);
    s.socket_drops = load(&h->socket_drops);
    s.spare_words = load(&h->spare_words);
    s.stale_slices = load(&h->stale_slices);
    s.stale_lines = load(&h->stale_lines);
    s.flattened = load(&h->flattened);
    s.saturated = load(&h->saturated);
    s.overruns = load(&h->overruns);
    s.run_started = load(&h->run_started_ns) / 1e9;
    s.last_any = load(&h->last_any_ns) / 1e9;
    s.flatten_active = load(&h->flatten_active) != 0;
    return s;
}

void StreamClient::write_control(w1700k_stream_header *h)
{
    store(&h->radio_sample_rate, sample_rate_);
    store(&h->radio_capture_rate, capture_rate_);
    store(&h->radio_filter_hz, filter_hz_);
    store(&h->flatten, flatten_);
}

void StreamClient::restart_run()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (view_) __atomic_fetch_add(&view_->header()->restart_requests, uint64_t(1), __ATOMIC_ACQ_REL);
}

void StreamClient::set_radio(double sample_rate, double capture_rate, double filter_hz)
{
    std::lock_guard<std::mutex> lock(mutex_);
    sample_rate_ = uint64_t(std::llround(sample_rate));
    capture_rate_ = uint64_t(std::llround(capture_rate));
    filter_hz_ = uint64_t(std::llround(filter_hz));
    if (view_) write_control(view_->header());
}

void StreamClient::set_flatten(bool on)
{
    std::lock_guard<std::mutex> lock(mutex_);
    flatten_ = on;
    if (view_) write_control(view_->header());
}

bool StreamClient::flatten() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return flatten_ != 0;
}
