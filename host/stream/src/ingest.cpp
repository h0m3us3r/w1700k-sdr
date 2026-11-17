// SPDX-License-Identifier: MIT
#include "ingest.h"

#include <chrono>
#include <cstring>
#include <mutex>
#include <stdexcept>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "awake.h"
#include "hw/mt7996/iq_reduce.h"

namespace {

// struct w1700k_sdr_export_header: le32 magic "SDR1", le16 offset, le16 slice_bytes, le64 seq.
// A reduced slice (iq_reduce.h) comes under "SDR2": the same fields, with the slice's format in
// bits [55:48] of the last and its scale code in [63:56], above a 48-bit seq. A datagram holds one
// such record, or several whole slices back to back; a record shorter than the rest of its slice
// is a piece of it and ends the datagram. A completion of 3588 bytes under "SDR1" whose last word
// is the radio-reduced trailer is a chunk the radio reduced itself (a split stream).
constexpr uint32_t kMagic = 0x31524453, kMagicReduced = 0x32524453;
constexpr size_t kHeader = 16;
constexpr unsigned kPartials = 4;                // slices a receiver assembles from pieces at once
constexpr unsigned kBatch = 64;
constexpr size_t kDatagram = 9216;
constexpr size_t kRawBytes = kSliceSamples * 4;
constexpr size_t kPackedBytes = kSliceSamples / 8 * 7 * 4;
constexpr size_t kMinSliceBytes = 4096;          // shorter completions are firmware events
constexpr uint64_t kRunGapNs = 1000000000;       // silence that ends a run
constexpr uint64_t kRunJumpBack = 1 << 20;       // slices back: a new run (the driver was reloaded)
constexpr uint64_t kRunJumpForward = uint64_t(1) << 39;  // keeps slot indices below the tag's run field
// A slice this far ahead of the newest one filed is not the next of its stream: within a run no
// second passes without a slice (kRunGapNs), and at 320 Msps a second is 312,500 of them. One
// datagram saying so is not believed (it was once: a single forged index then put every real
// slice a ring behind "the newest", and they were dropped as late for good); kFarConfirm of them
// in a row at one receiver, agreeing with each other, are a stream that moved, and begin a run.
constexpr uint64_t kRunJumpAhead = 1 << 20;
constexpr unsigned kFarConfirm = 16;

uint64_t now_ns()
{
    return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
                        std::chrono::steady_clock::now().time_since_epoch()).count());
}

// A run's newest slot filled is kept in one word with the run it belongs to in the bits above
// it, as a slot's tag has them (the slots of a run stay below kRunJumpForward). The receive
// threads each read the run and then file a slice; one that read it just before another thread
// began a new run is still filing a slice of the old one, at an index of the old one. Raising a
// bare "newest" with that index made the new run look that far along: every slice of the new
// stream was then older than the ring holds, and was dropped as late until its own numbering
// caught up. With the run in the word, such a thread changes nothing.
constexpr unsigned kNewestBits = 40;
constexpr uint64_t kNewestMask = (uint64_t(1) << kNewestBits) - 1;

uint64_t placed(uint64_t run, uint64_t newest) { return run << kNewestBits | newest; }

// The newest slot of `run` as the word `position` has it: nothing, if it is of another run.
uint64_t newest_of(uint64_t position, uint64_t run)
{
    return (position ^ placed(run, 0)) >> kNewestBits ? 0 : position & kNewestMask;
}

// Raises the newest slot to `newest`, while the word is still of `run`.
void advance(std::atomic<uint64_t> &a, uint64_t run, uint64_t newest)
{
    const uint64_t v = placed(run, newest);
    uint64_t o = a.load(std::memory_order_relaxed);
    while (!((o ^ v) >> kNewestBits) && v > o && !a.compare_exchange_weak(o, v, std::memory_order_acq_rel))
        ;
}

std::mutex run_mutex;

}  // namespace

SampleRing::SampleRing(unsigned capacity_slices)
    : cut_(new std::atomic<uint16_t>[capacity_slices]), slices_(capacity_slices),
      words_(size_t(capacity_slices) * kSliceSamples), tags_(new std::atomic<uint64_t>[capacity_slices])
{
    if (!capacity_slices || (capacity_slices & (capacity_slices - 1)))
        throw std::runtime_error("ring capacity must be a power of two");
    for (unsigned i = 0; i < capacity_slices; ++i) {
        tags_[i].store(0, std::memory_order_relaxed);
        cut_[i].store(kNone, std::memory_order_relaxed);
    }
}

void SampleRing::claim(uint64_t index)
{
    cut_[index & (slices_ - 1)].store(kNone, std::memory_order_relaxed);
    tags_[index & (slices_ - 1)].store(0, std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_release);
}

// The words of a slice as they came are bytes: a record that follows one of a length that is not
// a multiple of four (an event's) does not start on a word, so they are never read as uint32_t
// in place.
void SampleRing::put(uint64_t run, uint64_t index, const uint8_t *words)
{
    claim(index);
    std::memcpy(&words_[(index & (slices_ - 1)) * kSliceSamples], words, kSliceSamples * 4);
    tags_[index & (slices_ - 1)].store(tag_of(run, index), std::memory_order_release);
}

// Packed slices back to raw words: of each 8 samples, the 8th's 28 bits sit in the spare bits
// [31:30] and [15:14] of the other 7 (o_k = w_k | ((w7 << (14 - 2k)) & 0xc000c000)).
void SampleRing::put_packed(uint64_t run, uint64_t index, const uint8_t *bytes)
{
    claim(index);
    uint32_t *out = &words_[(index & (slices_ - 1)) * kSliceSamples];
    for (unsigned g = 0; g < kSliceSamples / 8; ++g, bytes += 7 * 4, out += 8) {
        uint32_t packed[7], tail = 0;
        std::memcpy(packed, bytes, sizeof(packed));
        for (unsigned k = 0; k < 7; ++k) {
            out[k] = packed[k] & ~0xc000c000u;
            tail |= (packed[k] & 0xc000c000u) >> (14 - 2 * k);
        }
        out[7] = tail;
    }
    tags_[index & (slices_ - 1)].store(tag_of(run, index), std::memory_order_release);
}

void SampleRing::put_reduced(uint64_t run, uint64_t index, unsigned format, unsigned scale, const uint8_t *reduced)
{
    claim(index);
    iq_reduce_decode(format, scale, reduced, &words_[(index & (slices_ - 1)) * kSliceSamples]);
    cut_[index & (slices_ - 1)].store(format == 14 ? uint16_t(scale) : kNone, std::memory_order_relaxed);
    tags_[index & (slices_ - 1)].store(tag_of(run, index), std::memory_order_release);
}

// A radio chunk's range against the least its unit-reduced neighbour's peak can have been. Called
// by whichever of the two is filed second; the one filed first found the other missing.
bool SampleRing::suspect(uint64_t run, uint64_t index) const
{
    auto cut = [&](uint64_t i) -> uint16_t {
        return tag(i) == tag_of(run, i) ? cut_[i & (slices_ - 1)].load(std::memory_order_relaxed) : kNone;
    };
    auto wraps = [](uint16_t radio, uint16_t unit) {
        return radio != kNone && unit != kNone && (radio & kRadio) && !(unit & kRadio) &&
               iq_reduce_peak_floor(14, unit) > iq_radio14_range(radio & 7);
    };
    const uint16_t here = cut(index), before = index ? cut(index - 1) : kNone, after = cut(index + 1);
    if (here == kNone) return false;
    return here & kRadio ? wraps(here, before) || wraps(here, after) : wraps(before, here) || wraps(after, here);
}

// A radio-reduced chunk (iq_reduce.h iq_radio14_*): false where its trailer says it is not one.
bool SampleRing::put_radio14(uint64_t run, uint64_t index, const uint8_t *chunk)
{
    claim(index);
    const int shift = iq_radio14_decode(chunk, &words_[(index & (slices_ - 1)) * kSliceSamples]);
    if (shift < 0) return false;
    cut_[index & (slices_ - 1)].store(uint16_t(kRadio | shift), std::memory_order_relaxed);
    tags_[index & (slices_ - 1)].store(tag_of(run, index), std::memory_order_release);
    return true;
}

void SampleRing::put_event(uint64_t run, uint64_t index)
{
    tags_[index & (slices_ - 1)].store(event_tag_of(run, index), std::memory_order_release);
}

struct Ingest::Worker {
    int fd = -1;
    std::thread thread;
    std::vector<uint8_t> buffers = std::vector<uint8_t>(kBatch * kDatagram);
    // Slices arriving in pieces (a sender keeps one slice's pieces on one flow, so on one receiver,
    // in order): the header of the first, then the bytes so far.
    struct Partial {
        uint64_t word = 0;                       // the header's last field: names the slice
        uint32_t magic = 0, bytes = 0, got = 0;  // got 0: free
        uint64_t used = 0;
        alignas(8) uint8_t data[kRawBytes + 8];
    };
    std::vector<Partial> partials = std::vector<Partial>(kPartials);
    uint64_t partial_clock = 0, given_up = ~uint64_t(0);  // the last slice counted in `pieces`
    uint64_t far_seq = 0;                        // the last slice far ahead of its stream (kRunJumpAhead),
    unsigned far_count = 0;                      // and how many such in a row agreed with each other
    // Written by this worker only, read by stats().
    std::atomic<uint64_t> datagrams{0}, bytes{0}, slices{0}, packed{0}, events{0}, pieces{0}, bad{0}, late{0},
        duplicates{0}, drops{0}, before{0}, spare{0}, stale{0}, stale_lines{0}, reduced{0}, suspect{0};
};

Ingest::Ingest(SampleRing &ring, const std::string &bind_ip, unsigned port, unsigned threads, const std::string &unit_ip)
    : ring_(ring), bind_ip_(bind_ip), port_(port), threads_(threads)
{
    in_addr check;
    if (inet_pton(AF_INET, bind_ip.c_str(), &check) != 1) throw std::runtime_error("not an IPv4 address: " + bind_ip);
    if (!unit_ip.empty()) {
        if (inet_pton(AF_INET, unit_ip.c_str(), &check) != 1 || !check.s_addr)
            throw std::runtime_error("not an IPv4 address: " + unit_ip);
        unit_ = check.s_addr;
    }
}

// Binds the sockets and starts receiving: not before, or the kernel would queue (and then
// drop) what came while nothing read.
void Ingest::start()
{
    if (!workers_.empty()) return;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(uint16_t(port_));
    inet_pton(AF_INET, bind_ip_.c_str(), &address.sin_addr);
    for (unsigned i = 0; i < threads_; ++i) {
        auto w = std::make_unique<Worker>();
        int one = 1, rcvbuf = 48 << 20;
        timeval tick{0, 100000};
        w->fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);  // not inherited by the controller
        if (w->fd < 0 || setsockopt(w->fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one)) ||
            setsockopt(w->fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf)) ||
            setsockopt(w->fd, SOL_SOCKET, SO_RXQ_OVFL, &one, sizeof(one)) ||
            setsockopt(w->fd, SOL_SOCKET, SO_RCVTIMEO, &tick, sizeof(tick)) ||
            bind(w->fd, reinterpret_cast<sockaddr *>(&address), sizeof(address))) {
            std::string why = std::strerror(errno);
            if (w->fd >= 0) close(w->fd);
            throw std::runtime_error("UDP " + bind_ip_ + ":" + std::to_string(port_) + ": " + why);
        }
        workers_.push_back(std::move(w));
    }
    for (auto &w : workers_) w->thread = std::thread([this, p = w.get()] { work(*p); });
}

Ingest::~Ingest()
{
    stopping_ = true;
    for (auto &w : workers_) {
        if (w->thread.joinable()) w->thread.join();
        close(w->fd);
    }
}

void Ingest::work(Worker &w)
{
    mmsghdr msgs[kBatch];
    iovec iov[kBatch];
    sockaddr_in from[kBatch];
    char control[kBatch][CMSG_SPACE(sizeof(uint32_t))];
    while (!stopping_.load(std::memory_order_relaxed)) {
        for (unsigned k = 0; k < kBatch; ++k) {
            iov[k] = {&w.buffers[k * kDatagram], kDatagram};
            msgs[k].msg_hdr = msghdr{};
            msgs[k].msg_hdr.msg_name = &from[k];
            msgs[k].msg_hdr.msg_namelen = sizeof(from[k]);
            msgs[k].msg_hdr.msg_iov = &iov[k];
            msgs[k].msg_hdr.msg_iovlen = 1;
            msgs[k].msg_hdr.msg_control = control[k];
            msgs[k].msg_hdr.msg_controllen = sizeof(control[k]);
        }
        int n = recvmmsg(w.fd, msgs, kBatch, MSG_WAITFORONE, nullptr);
        if (n <= 0) continue;
        awake::needed();
        // A second of silence ends the run: the next slice starts another. Signed: another
        // thread may have stamped a later time between our clock read and the exchange.
        uint64_t t = now_ns(), previous = last_ns_.exchange(t, std::memory_order_acq_rel);
        bool fresh = previous && int64_t(t - previous) > int64_t(kRunGapNs);
        for (int k = 0; k < n; ++k) {
            // Told which unit it serves (--unit), it takes no other sender's word for anything:
            // not a slice, and not that the stream is alive. The socket's losses are still read.
            if (unit_ && (msgs[k].msg_hdr.msg_namelen < sizeof(sockaddr_in) || from[k].sin_family != AF_INET ||
                          from[k].sin_addr.s_addr != unit_))
                w.bad.fetch_add(1, std::memory_order_relaxed);
            else
                take(w, &w.buffers[k * kDatagram], msgs[k].msg_len, fresh && k == 0, t);
            for (cmsghdr *c = CMSG_FIRSTHDR(&msgs[k].msg_hdr); c; c = CMSG_NXTHDR(&msgs[k].msg_hdr, c))
                if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SO_RXQ_OVFL) {
                    uint32_t drops;
                    std::memcpy(&drops, CMSG_DATA(c), sizeof(drops));
                    w.drops.store(drops, std::memory_order_relaxed);
                }
        }
    }
}

// Firmware events are filed as event slots rather than dropped: the scheduler steps over them
// without counting a loss, and the samples either side stay contiguous. A completion the unit
// does not send at all (an empty one) cannot be told from a lost datagram and counts as lost.
void Ingest::take(Worker &w, const uint8_t *data, size_t n, bool fresh, uint64_t t)
{
    if (fresh) new_run_.store(true, std::memory_order_release);
    w.datagrams.fetch_add(1, std::memory_order_relaxed);
    w.bytes.fetch_add(n, std::memory_order_relaxed);
    do {
        const size_t used = record(w, data, n, t);
        data += used;
        n -= used;
    } while (n);
}

// One record at the head of a datagram's remaining `n` bytes; returns the bytes it took (all of
// them when it is not a whole slice, or not a record at all).
size_t Ingest::record(Worker &w, const uint8_t *data, size_t n, uint64_t t)
{
    uint32_t magic;
    uint16_t offset, slice_bytes;
    uint64_t seq;
    if (n < kHeader) {
        w.bad.fetch_add(1, std::memory_order_relaxed);
        return n;
    }
    std::memcpy(&magic, data, 4);
    std::memcpy(&offset, data + 4, 2);
    std::memcpy(&slice_bytes, data + 6, 2);
    std::memcpy(&seq, data + 8, 8);
    // A whole slice may be followed by another record; anything else is the datagram's last.
    const bool whole = !offset && n - kHeader >= slice_bytes;
    const size_t len = whole ? slice_bytes : n - kHeader, taken = kHeader + len;
    const bool reduced = magic == kMagicReduced;
    const unsigned format = reduced ? unsigned(seq >> 48) & 0xff : 0, scale = reduced ? unsigned(seq >> 56) : 0;
    const bool event = !reduced && slice_bytes < kMinSliceBytes;
    if ((magic != kMagic && !reduced) || offset + len > slice_bytes ||
        (reduced ? slice_bytes != iq_reduce_bytes(format)
                 : !event && slice_bytes != kRawBytes && slice_bytes != kPackedBytes)) {
        w.bad.fetch_add(1, std::memory_order_relaxed);
        return taken;
    }
    const uint8_t *payload = data + kHeader;
    if (offset || len != slice_bytes) {
        // A piece: appended to its slice, which is filed once whole. A slice whose pieces do
        // not all come, in order, is never filed and so counts as lost.
        Worker::Partial *p = nullptr, *spare = &w.partials[0];
        for (auto &q : w.partials) {
            if (q.got && q.word == seq && q.magic == magic && q.bytes == slice_bytes) p = &q;
            // a free entry before an unfinished one, the longest untouched of those
            if (!q.got ? spare->got || q.used < spare->used : spare->got && q.used < spare->used) spare = &q;
        }
        if (!p) {
            if (offset) {                        // the rest of a slice given up, or one whose start was lost
                if (w.given_up != seq) w.pieces.fetch_add(1, std::memory_order_relaxed);
                w.given_up = seq;
                return taken;
            }
            p = spare;
            if (p->got) w.pieces.fetch_add(1, std::memory_order_relaxed);  // displaced unfinished
            p->word = seq, p->magic = magic, p->bytes = slice_bytes, p->got = 0;
        }
        if (offset != p->got) {                  // a piece is missing: the slice is given up
            w.pieces.fetch_add(1, std::memory_order_relaxed);
            w.given_up = seq;
            p->got = 0;
            return taken;
        }
        std::memcpy(p->data + offset, payload, len);
        p->got += uint32_t(len);
        p->used = ++w.partial_clock;
        if (p->got < slice_bytes) return taken;
        p->got = 0;
        payload = p->data;
    }
    if (reduced) seq &= (uint64_t(1) << 48) - 1;

    uint64_t run = run_.load(std::memory_order_acquire), base = base_.load(std::memory_order_acquire);
    auto jumped = [](uint64_t seq, uint64_t base) {
        return seq < base ? base - seq > kRunJumpBack : seq - base > kRunJumpForward;
    };
    if (!run || jumped(seq, base) || new_run_.load(std::memory_order_acquire)) {
        std::lock_guard<std::mutex> lock(run_mutex);
        if (run_.load() == run && (new_run_.exchange(false) || !run || jumped(seq, base_.load()))) {
            base_.store(seq);
            newest_.store(placed(run + 1, 0));
            started_ns_.store(t);
            run_.store(run + 1, std::memory_order_release);
        }
        run = run_.load(std::memory_order_acquire);
        base = base_.load(std::memory_order_acquire);
    }
    // Far ahead of the newest slice filed (kRunJumpAhead): not filed, and counted as bad, until
    // enough of them say the same. Then the stream is there now, and a run begins at it.
    if (seq >= base && seq - base > newest_of(newest_.load(std::memory_order_relaxed), run) + kRunJumpAhead) {
        const uint64_t apart = seq > w.far_seq ? seq - w.far_seq : w.far_seq - seq;
        w.far_count = w.far_count && apart <= kRunJumpAhead ? w.far_count + 1 : 1;
        w.far_seq = seq;
        if (w.far_count < kFarConfirm) {
            w.bad.fetch_add(1, std::memory_order_relaxed);
            return taken;
        }
        std::lock_guard<std::mutex> lock(run_mutex);
        if (run_.load() == run) {
            base_.store(seq);
            newest_.store(placed(run + 1, 0));
            started_ns_.store(t);
            run_.store(run + 1, std::memory_order_release);
        }
        run = run_.load(std::memory_order_acquire);
        base = base_.load(std::memory_order_acquire);
    }
    w.far_count = 0;
    // A completion a little before the run's first was overtaken on the way: late (or, in the
    // run's first second, from before this server joined the stream).
    if (seq < base) {
        (int64_t(t - started_ns_.load()) < int64_t(kRunGapNs) ? w.before : w.late).fetch_add(1, std::memory_order_relaxed);
        return taken;
    }
    const uint64_t index = seq - base;
    if (index + ring_.capacity_slices() < newest_of(newest_.load(std::memory_order_relaxed), run)) {
        w.late.fetch_add(1, std::memory_order_relaxed);
        return taken;
    }
    const uint64_t held = ring_.tag(index);
    if (held == SampleRing::tag_of(run, index) || held == SampleRing::event_tag_of(run, index)) {
        w.duplicates.fetch_add(1, std::memory_order_relaxed);
        return taken;
    }
    if (event && slice_bytes == IQ_RADIO14_BYTES && ring_.put_radio14(run, index, payload)) {
        // of a split stream: the half of the chunks the radio reduced itself
        w.slices.fetch_add(1, std::memory_order_relaxed);
        w.reduced.fetch_add(1, std::memory_order_relaxed);
        if (ring_.suspect(run, index)) w.suspect.fetch_add(1, std::memory_order_relaxed);
    } else if (event) {
        ring_.put_event(run, index);
        w.events.fetch_add(1, std::memory_order_relaxed);
    } else if (reduced) {
        ring_.put_reduced(run, index, format, scale, payload);
        w.slices.fetch_add(1, std::memory_order_relaxed);
        w.reduced.fetch_add(1, std::memory_order_relaxed);
        if (ring_.suspect(run, index)) w.suspect.fetch_add(1, std::memory_order_relaxed);
    } else if (slice_bytes == kRawBytes) {
        uint64_t spare = 0;
        for (unsigned k = 0; k < kSliceSamples; ++k) {
            uint32_t word;                       // not read in place: see SampleRing::put
            std::memcpy(&word, payload + 4 * k, 4);
            spare += (word & 0xc000c000u) != 0;
        }
        if (spare) w.spare.fetch_add(spare, std::memory_order_relaxed);
        ring_.put(run, index, payload);
        w.slices.fetch_add(1, std::memory_order_relaxed);
        check(w, run, index);
    } else {
        ring_.put_packed(run, index, payload);
        w.slices.fetch_add(1, std::memory_order_relaxed);
        w.packed.fetch_add(1, std::memory_order_relaxed);
        check(w, run, index);
    }
    advance(newest_, run, index + 1);
    return taken;
}

// A slice equal to the one a capture-ring lap earlier is what a reader that overtook the capture
// sends; an 8-sample line equal to the same line a lap earlier (28 bytes packed, 32 raw) is what a
// packer reading a stale D-cache line sends. Noise never repeats; a flat line (all eight samples
// equal, which the radio does output) can, so it is not compared.
void Ingest::check(Worker &w, uint64_t run, uint64_t index)
{
    if (index < kLap || ring_.tag(index - kLap) != SampleRing::tag_of(run, index - kLap)) return;
    const uint32_t *now = ring_.slice(index), *then = ring_.slice(index - kLap);
    uint64_t lines = 0, stale = 0;
    for (unsigned g = 0; g < kSliceSamples; g += 8) {
        bool flat = true;
        for (unsigned k = 1; k < 8 && flat; ++k) flat = now[g + k] == now[g];
        if (flat) continue;
        ++lines;
        stale += std::memcmp(now + g, then + g, 32) == 0;
    }
    if (ring_.tag(index - kLap) != SampleRing::tag_of(run, index - kLap)) return;  // overwritten meanwhile
    if (stale) w.stale_lines.fetch_add(stale, std::memory_order_relaxed);
    if (lines && stale == lines) w.stale.fetch_add(1, std::memory_order_relaxed);
}

Ingest::Position Ingest::position() const
{
    for (;;) {
        // The word is of the next run for a moment before the run itself is: read again then.
        const uint64_t run = run_.load(std::memory_order_acquire), position = newest_.load(std::memory_order_acquire);
        if (!((position ^ placed(run, 0)) >> kNewestBits)) return {run, position & kNewestMask};
    }
}

void Ingest::restart_run() { new_run_.store(true, std::memory_order_release); }

Ingest::Stats Ingest::stats() const
{
    Stats s;
    for (const auto &w : workers_) {
        s.datagrams += w->datagrams.load(std::memory_order_relaxed);
        s.bytes += w->bytes.load(std::memory_order_relaxed);
        s.slices += w->slices.load(std::memory_order_relaxed);
        s.packed += w->packed.load(std::memory_order_relaxed);
        s.events += w->events.load(std::memory_order_relaxed);
        s.pieces += w->pieces.load(std::memory_order_relaxed);
        s.bad += w->bad.load(std::memory_order_relaxed);
        s.late += w->late.load(std::memory_order_relaxed);
        s.before_start += w->before.load(std::memory_order_relaxed);
        s.duplicates += w->duplicates.load(std::memory_order_relaxed);
        s.socket_drops += w->drops.load(std::memory_order_relaxed);
        s.spare_words += w->spare.load(std::memory_order_relaxed);
        s.stale_slices += w->stale.load(std::memory_order_relaxed);
        s.stale_lines += w->stale_lines.load(std::memory_order_relaxed);
        s.reduced += w->reduced.load(std::memory_order_relaxed);
        s.suspect += w->suspect.load(std::memory_order_relaxed);
    }
    const Position at = position();
    s.run = at.run;
    s.newest = at.newest;
    s.run_started = started_ns_.load() / 1e9;
    s.last_any = last_ns_.load() / 1e9;
    return s;
}
