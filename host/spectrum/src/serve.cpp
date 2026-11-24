// SPDX-License-Identifier: MIT
// w1700k-spectrum: the unit's exported stream as a live spectrum and waterfall in a web
// browser, with the receiver's controls.
//
// Threads:
//   ingest    UDP receive into the sample ring (Ingest, several)
//   spectrum  the frame scheduler and its FFT workers (SpectrumEngine)
//   web       the HTTP and WebSocket loop (the calling thread): the client's view, flow control,
//             status and the controller
//
// One browser is served at a time. A new connection takes over from the previous one, which
// is closed with code 4001. Every frame is fitted to what the client asked for (frequency
// range and number of bins) and sent only while fewer than a few frames are unacknowledged, so
// a slow or distant client gets fewer frames rather than a growing backlog; the frames it
// misses are merged into the next one it gets.
//
// The receiver is controlled through a child process (Controller). Its state says what the
// stream is (centre frequency, sample rate), which labels every frame from then on.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <deque>
#include <mutex>
#include <set>
#include <stdexcept>
#include <thread>

#include "controller.h"
#include "../../stream/src/placement.h"
#include "stream.h"
#include "json.h"
#include "serve.h"
#include "spectrum.h"
#include "web.h"

using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;

namespace {

constexpr int kProtocolVersion = 3;
constexpr uint16_t kReplaced = 4001;       // close code: another client took over
constexpr size_t kSocketBacklog = 32768;   // bytes queued for a client before frames wait
constexpr unsigned kMaxBins = 8192;
constexpr float kStepDb = 0.5f;            // quantisation of the frames sent

// The radio's bands.
struct Band {
    int index;
    const char *name;
};
const std::vector<Band> kBands = {{0, "2.4 GHz"}, {1, "5 GHz"}, {2, "6 GHz"}};
// Rates: the stream's sample rate and the radio's capture rate, samples/s, and the bands that have
// them. 640/2 captures 640 Msps on band 2 and streams every other sample: flat to about +-155 MHz,
// but signals 160..320 MHz from the LO fold in. Analog filter widths, MHz: on every band. Rate,
// filter and LO are independent of each other.
struct Rate {
    const char *label;
    double sample_rate, capture_rate;
    std::vector<int> bands;
};
const std::vector<Rate> kRates = {
    {"40", 40e6, 40e6, {0, 1, 2}},     {"80", 80e6, 80e6, {0, 1, 2}}, {"160", 160e6, 160e6, {0, 1, 2}},
    {"320", 320e6, 320e6, {0, 1, 2}}, {"640/2", 320e6, 640e6, {2}},
};
const std::vector<unsigned> kFilters = {20, 40, 80, 160, 320};
constexpr int kMaxLna = 4, kMaxLpf = 9;                          // gain steps: ~6 dB and ~2 dB

std::atomic<bool> stop_requested{false}, dump_requested{false};
void on_signal(int) { stop_requested = true; }
void on_dump(int) { dump_requested = true; }

double wall_ms()
{
    return std::chrono::duration<double, std::milli>(std::chrono::system_clock::now().time_since_epoch()).count();
}

double steady_s() { return std::chrono::duration<double>(Clock::now().time_since_epoch()).count(); }

// A whole number in [low, high] from a client message.
int integer_field(const JsonValue &m, const char *key, int low, int high)
{
    const JsonValue *v = m.find(key);
    if (!v || !v->is_number() || v->number != std::floor(v->number) || v->number < low || v->number > high)
        throw std::runtime_error(std::string(key) + " must be a whole number from " + std::to_string(low) + " to " +
                                 std::to_string(high));
    return int(v->number);
}

std::string mhz(double hz)
{
    char text[32];
    std::snprintf(text, sizeof(text), "%.3f", hz / 1e6);
    return text;
}

// The browser being served.
struct Viewer {
    double start_hz = 0, stop_hz = 0;  // requested range; stop <= start: the whole span
    unsigned bins = 1024;
    bool auto_bins = true;             // fewer bins when the link cannot carry these
    unsigned bins_cap = 1024;          // that limit (0: none); links start modestly
    double max_fps = 0;                // 0: every frame produced
    double max_kbps = 0;               // 0: no limit

    // Frames sent and not yet acknowledged, and the window that bounds them: it grows while
    // round trips stay near those of an empty link and shrinks when they show a queue, so
    // frames are never late for long.
    struct Sent {
        uint32_t sequence;
        Clock::time_point time;
        uint64_t bytes;
        bool alone;                    // nothing else was in flight
    };
    uint32_t sequence = 0;
    std::deque<Sent> in_flight;
    uint64_t in_flight_bytes = 0, delivered = 0, last_frame_bytes = 0;
    double window = 0;                 // bytes allowed in flight; 0 until the first frame
    Clock::time_point cut_at;          // last reduction
    double rtt = 0, rtt_min = 0;       // seconds
    Clock::time_point rtt_min_at = Clock::now();
    bool probing = false;              // one frame at a time, to measure rtt_min again
    Clock::time_point last_send;
    double tokens = 0;                 // allowance under max_kbps, bytes
    Clock::time_point tokens_at = Clock::now();

    // The next frame to send, of each channel: a view of both channels of a two-channel stream
    // has one waiting of each, sent together; any other has channel 0's or channel 1's alone.
    std::shared_ptr<const SpectrumFrame> pending[2];
    std::shared_ptr<SpectrumFrame> merged[2];      // `pending`, when it is this viewer's own merge
    unsigned pending_frames[2] = {0, 0};           // frames combined into it

    Clock::time_point mark = Clock::now();         // per-second statistics
    unsigned frames_sent = 0, frames_merged = 0;
    uint64_t bytes_mark = 0, delivered_mark = 0;
    double fps = 0, kbps = 0, merged_rate = 0;
    double delivered_rate = 0;         // frame bytes acknowledged per second
    Clock::time_point last_status;
    size_t last_status_bytes = 0;
    std::map<std::string, std::string> sections;  // status sections as last sent
    Clock::time_point sections_at;                // when they were all last sent
};

class Service {
public:
    explicit Service(const ServeOptions &options)
        : options_(options),
          stream_(options.stream),
          web_(options.listen, options.web_dir,
               {[this](unsigned c) { on_open(c); }, [this](unsigned c, const std::string &m) { on_text(c, m); },
                [this](unsigned c) { on_close(c); }, [this] { on_wake(); }, [this] { on_tick(); }},
               WebServer::Access{options.hosts, options.origins, options.behind_proxy, options.token, options.token_value}),
          engine_(stream_, Tuning{options.center_hz, options.sample_rate}, options.fft_threads,
                  [this](std::shared_ptr<const SpectrumFrame> frame) {
                      {
                          std::lock_guard<std::mutex> lock(frames_mutex_);
                          frames_.push_back(std::move(frame));
                          while (frames_.size() > 64) frames_.pop_front();
                      }
                      web_.wake();
                  }),
          tuning_{options.center_hz, options.sample_rate}
    {
        if (!options.controller.empty())
            controller_ = std::make_unique<Controller>(
                options.controller, [this] { web_.wake(); },
                Controller::Listener{[this](const JsonValue &state) { on_controller_state(state); },
                                     [this](const std::string &level, const std::string &text) { notice(level, text); },
                                     [this] { status_due_ = true; }});
    }

    ~Service()
    {
        if (dump_thread_.joinable()) dump_thread_.join();
    }

    const std::string &address() const { return web_.address(); }
    WebServer &web() { return web_; }
    void run(const std::atomic<bool> &stop) { web_.run(stop); }

private:
    // ---- the receiver ----------------------------------------------------------------------

    void on_controller_state(const JsonValue &state)
    {
        const JsonValue *center = state.at("stream.center_hz"), *rate = state.at("stream.sample_rate");
        if (center && rate && center->is_number() && rate->is_number() && center->number > 0 && rate->number > 0) {
            const Tuning t{center->number, rate->number};
            // While a tune or configure runs, only a reported change is news; its reply settles
            // the rest (end_tuning_op).
            if (t != tuning_ || !tuning_ops_) engine_.set_tuning(t);
            tuning_ = t;
        }
        const JsonValue *running = state.at("stream.running");
        const bool now_running = running && running->is_boolean() && running->boolean;
        if (streaming_ && !now_running) stream_.restart_run();  // what comes next is a new stream
        streaming_ = now_running;
        // A running stream's channels: the stream service was told by whoever started it, and is
        // told again from here should it restart. A stopped stream's are nobody's to say.
        const JsonValue *chans = state.at("stream.channels");
        const unsigned channels = now_running && chans && chans->is_number() && chans->number == 2 ? 2 : 1;
        if (now_running) stream_.set_channels(channels);
        if (channels != stream_channels_) {
            stream_channels_ = channels;
            status_due_ = true;
        }
        // What the stream service needs to know to correct the roll-off.
        auto number = [&](const char *path) {
            const JsonValue *v = state.at(path);
            return v && v->is_number() ? v->number : 0.0;
        };
        const double sample = number("radio.sample_rate"), capture = number("radio.capture_rate");
        const double filter = number("rf.filter_mhz") * 1e6;
        if (sample != radio_rate_ || capture != radio_capture_ || filter != radio_filter_) {
            radio_rate_ = sample;
            radio_capture_ = capture ? capture : sample;
            radio_filter_ = filter;
            stream_.set_radio(radio_rate_, radio_capture_, radio_filter_);
            status_due_ = true;
        }
    }

    // The correction applies where the analog filter is as wide as the sample rate and the stream
    // is not 640/2, which is flat already (w1700k-stream decides; the page shows the same).
    bool flatten_available() const
    {
        static const double kCorrected[] = {40e6, 80e6, 160e6, 320e6};
        return radio_rate_ > 0 && radio_capture_ == radio_rate_ && radio_filter_ == radio_rate_ &&
               std::find(std::begin(kCorrected), std::end(kCorrected), radio_rate_) != std::end(kCorrected);
    }

    // Frames of both channels of a two-channel stream are being made (the page shows both).
    bool shows_both() const
    {
        return stream_channels_ == 2 && engine_.settings().channel == SpectrumSettings::kBothChannels;
    }

    void require_controller() const
    {
        if (!controller_) throw std::runtime_error("receiver control is off: the server runs with --no-controller");
        if (!controller_->alive()) throw std::runtime_error("the controller is not running; it is restarted shortly");
        if (!controller_->ready()) throw std::runtime_error("the controller has not reported the receiver's state yet");
    }

    void require_idle() const
    {
        if (controller_->busy())
            throw std::runtime_error("the receiver is busy (" + controller_->busy_text() + "): try again when it is done");
    }

    // Requests that may change what the samples are: frames are dropped from the newest slice
    // on, until the controller's state says what they are again.
    void begin_tuning_op()
    {
        ++tuning_ops_;
        engine_.begin_change();
    }

    void end_tuning_op(const std::string &what, const std::string &error)
    {
        if (tuning_ops_) --tuning_ops_;
        // A controller that died mid-way leaves the radio unknown: frames stay dropped until
        // its successor reports the state.
        if (controller_->alive()) engine_.set_tuning(tuning_);
        if (!error.empty()) notice("error", what + " failed: " + error);
    }

    Controller::Done report(const std::string &what)
    {
        return [this, what](const std::string &error) {
            if (!error.empty()) notice("error", what + " failed: " + error);
        };
    }

    // `channels` 2: chains 0 and 1 of the band together (host/w1700k: start, channels).
    Controller::Op start_op(unsigned channels = 1)
    {
        return {"start", channels == 2 ? ",\"channels\":2" : "", [this] { stream_.restart_run(); }, report("start")};
    }

    Controller::Op stop_op() { return {"stop", "", nullptr, report("stop")}; }

    void receiver_run(bool on, unsigned channels)
    {
        require_controller();
        require_idle();
        if (channels != 1 && channels != 2) throw std::runtime_error("channels must be 1 or 2");
        if (on == streaming_) return;
        if (!on) {
            controller_->submit({stop_op()});
            return;
        }
        controller_->submit({start_op(channels)});
    }

    // One `set` message may carry several groups of settings, applied in this order; the first
    // that is refused ends it.
    void receiver_set(const JsonValue &m)
    {
        require_controller();
        if (m.has("flatten")) {
            const JsonValue *v = m.at("flatten");
            if (!v || !v->is_boolean()) throw std::runtime_error("flatten must be true or false");
            stream_.set_flatten(v->boolean);
            status_due_ = true;
        }
        const bool configure = m.has("band") || m.has("sample_rate");
        if (configure) set_configuration(m);
        if (m.has("filter")) set_filter(m);
        if (m.has("filter_follow")) set_filter_follow(m);
        if (m.has("agc") || m.has("lna") || m.has("lpf")) set_gain(m);
        if (m.has("lo")) tune(m.number_or("lo", NAN));
    }

    void set_configuration(const JsonValue &m)
    {
        const int band = integer_field(m, "band", 0, int(kBands.size()) - 1);
        const double rate = m.number_or("sample_rate", NAN), capture = m.number_or("capture_rate", rate);
        const auto r = std::find_if(kRates.begin(), kRates.end(), [&](const Rate &x) {
            return x.sample_rate == rate && x.capture_rate == capture;
        });
        if (r == kRates.end())
            throw std::runtime_error("sample_rate must be 40, 80, 160 or 320 Msps (640/2: 320 captured at 640)");
        if (std::find(r->bands.begin(), r->bands.end(), band) == r->bands.end())
            throw std::runtime_error(std::string("the ") + kBands[size_t(band)].name + " band has no " + r->label);
        require_idle();
        controller_->submit({{"configure",
                              ",\"band\":" + std::to_string(band) + ",\"sample_rate\":" + json_number(rate) +
                                  ",\"capture_rate\":" + json_number(capture),
                              [this] { begin_tuning_op(); },
                              [this](const std::string &error) { end_tuning_op("configure", error); }}});
    }

    void set_filter(const JsonValue &m)
    {
        const int width = integer_field(m, "filter", 20, 320);
        if (std::find(kFilters.begin(), kFilters.end(), unsigned(width)) == kFilters.end())
            throw std::runtime_error("filter must be 20, 40, 80, 160 or 320 MHz");
        require_idle();
        controller_->submit({{"filter", ",\"mhz\":" + std::to_string(width), nullptr, report("filter")}}, "filter");
    }

    // Whether every band's analog filter follows its sample rate (a filter set by hand ends that).
    void set_filter_follow(const JsonValue &m)
    {
        const JsonValue *v = m.at("filter_follow");
        if (!v || !v->is_boolean()) throw std::runtime_error("filter_follow must be true or false");
        require_idle();
        controller_->submit({{"filter_follow", std::string(",\"on\":") + (v->boolean ? "true" : "false"), nullptr,
                              report("filter")}},
                            "filter_follow");
    }

    void set_gain(const JsonValue &m)
    {
        std::string fields;
        if (m.boolean_or("agc", false)) {
            fields = ",\"agc\":true";
        } else {
            const int lna = integer_field(m, "lna", 0, kMaxLna), lpf = integer_field(m, "lpf", 0, kMaxLpf);
            fields = ",\"lna\":" + std::to_string(lna) + ",\"lpf\":" + std::to_string(lpf);
        }
        require_idle();
        controller_->submit({{"gain", fields, nullptr, report("gain")}}, "gain");
    }

    void tune(double hz)
    {
        if (!std::isfinite(hz)) throw std::runtime_error("lo must be a frequency in Hz");
        const JsonValue *range = controller_->state().at("rf.lo_range_hz");
        if (!range || range->kind != JsonValue::Array || range->items.size() != 2 || !range->items[0].is_number() ||
            !range->items[1].is_number())
            throw std::runtime_error("the LO range is not known yet: configure the radio first");
        const double low = range->items[0].number, high = range->items[1].number;
        if (hz < low || hz > high)
            throw std::runtime_error(mhz(hz) + " MHz is outside the LO range of this band, " + mhz(low) + " to " +
                                     mhz(high) + " MHz");
        require_idle();
        controller_->submit({{"tune", ",\"hz\":" + json_number(std::round(hz)), [this] { begin_tuning_op(); },
                              [this](const std::string &error) { end_tuning_op("tune", error); }}},
                            "tune");
    }

    void notice(const std::string &level, const std::string &text)
    {
        if (client_)
            web_.send_text(client_, "{\"t\":\"notice\",\"level\":" + json_quote(level) + ",\"text\":" + json_quote(text) + "}");
    }

    // ---- SIGUSR1 snapshot ------------------------------------------------------------------

    // A snapshot of the newest samples, a little behind the newest slice so they have all
    // arrived, as the stream serves them: complex int16 (I, Q) on the 14-bit scale, corrected
    // where the correction applied (flattened_slices in the .json), in sample order. A slice that
    // is lost, or is overwritten while copied, is left as zeros and named in the .json; a firmware
    // event's slot holds no samples and is left out.
    void dump()
    {
        const Tuning now = engine_.tuning();
        const std::shared_ptr<const StreamView> view = stream_.view();
        const StreamClient::Position p = stream_.position();
        // A two-channel run's slot holds half as many sample times, of both channels; the dump is
        // then a sample time at a time, channel 0's I and Q and channel 1's (as sdr-recv records).
        const unsigned per = kSliceSamples / p.channels;
        const uint64_t n = uint64_t(options_.dump_seconds * now.sample_rate / per);
        // The buffer first: filling it takes long enough for the ring to move on.
        std::vector<int16_t> samples(n * kSliceSamples * 2);
        if (!view || !p.run || !n || p.newest < n + 64 || n + 64 > view->capacity() * 3 / 4) {
            std::fprintf(stderr, "dump: not enough stream (or more than the ring holds)\n");
            return;
        }
        const uint64_t first = p.newest - 64 - n;
        std::vector<uint64_t> missing;
        uint64_t events = 0, kept = 0, flattened = 0;
        for (uint64_t k = 0; k < n; ++k) {
            const uint64_t index = first + k;
            uint64_t tag = view->tag(index);
            if (StreamView::holds_event(tag, p.run, index)) {
                ++events;
                continue;
            }
            int16_t *out = &samples[kept++ * kSliceSamples * 2];
            bool ok = StreamView::holds_samples(tag, p.run, index);
            if (ok) {
                if (p.channels == 2) {
                    const int16_t *a = view->slice(index), *b = a + 2 * per;
                    for (unsigned k2 = 0; k2 < per; ++k2) {
                        out[4 * k2] = a[2 * k2];
                        out[4 * k2 + 1] = a[2 * k2 + 1];
                        out[4 * k2 + 2] = b[2 * k2];
                        out[4 * k2 + 3] = b[2 * k2 + 1];
                    }
                } else {
                    std::memcpy(out, view->slice(index), kSliceSamples * 4);
                }
                std::atomic_thread_fence(std::memory_order_acquire);
                ok = view->tag(index) == tag;
                flattened += ok && (tag & W1700K_STREAM_TAG_FLAT);
            }
            if (!ok) {
                std::memset(out, 0, kSliceSamples * 4);
                missing.push_back(k);
            }
        }
        samples.resize(kept * kSliceSamples * 2);
        const std::optional<Tuning> held = engine_.tuning_over(p.run, first, first + n - 1);
        const Tuning t = held.value_or(now);
        const long stamp = long(std::chrono::duration_cast<std::chrono::milliseconds>(
                                    std::chrono::system_clock::now().time_since_epoch()).count());
        const std::string base = options_.dump_dir + "/w1700k-dump-" + std::to_string(stamp);
        FILE *f = std::fopen((base + ".bin").c_str(), "wbe");
        bool written = f && std::fwrite(samples.data(), 2, samples.size(), f) == samples.size();
        if (f && std::fclose(f)) written = false;
        std::string list;
        for (size_t i = 0; i < missing.size() && i < 64; ++i) list += (i ? "," : "") + std::to_string(missing[i]);
        FILE *j = std::fopen((base + ".json").c_str(), "we");
        if (j) {
            std::fprintf(j, "{\"center_hz\":%.3f,\"sample_rate\":%.3f,\"channels\":%u,\"tuning_steady\":%s,\"run\":%llu,"
                            "\"first_slice\":%llu,\"slices\":%llu,\"samples\":%llu,\"event_slices\":%llu,"
                            "\"missing_slices\":%zu,\"missing\":[%s],\"flattened_slices\":%llu,\"unix_ms\":%ld,"
                            "\"format\":\"%s\"}\n",
                         t.center_hz, t.sample_rate, p.channels, held ? "true" : "false",
                         (unsigned long long)StreamView::service_run(p.run), (unsigned long long)first,
                         (unsigned long long)n, (unsigned long long)(kept * per),
                         (unsigned long long)events, missing.size(), list.c_str(), (unsigned long long)flattened,
                         stamp,
                         p.channels == 2 ? "ci16, two channels: per sample time int16 I, Q of channel 0, then I, Q of "
                                           "channel 1; 14-bit scale (full scale 8192); samples counts sample times"
                                         : "ci16: int16 I then int16 Q per sample, 14-bit scale (full scale 8192)");
            if (std::fclose(j)) written = false;
        } else {
            written = false;
        }
        std::fprintf(stderr, "dump: %s.bin, %llu samples at %s MHz, %zu slices missing%s%s\n", base.c_str(),
                     (unsigned long long)(kept * per), mhz(t.center_hz).c_str(), missing.size(),
                     held ? "" : " (the tuning changed during it)", written ? "" : " (WRITE FAILED)");
    }

    // ---- the web client --------------------------------------------------------------------

    void on_open(unsigned client)
    {
        if (client_) web_.close(client_, kReplaced, "another browser took over");
        client_ = client;
        viewer_ = Viewer();
        std::string windows, bands, rates, filters;
        for (const auto &name : spectrum_windows())
            windows += std::string(windows.empty() ? "" : ",") + "{\"name\":" + json_quote(name) +
                       ",\"bandwidth\":" + json_number(spectrum_window_bandwidth(name)) + "}";
        for (const Band &b : kBands)
            bands += std::string(bands.empty() ? "" : ",") + "{\"band\":" + std::to_string(b.index) +
                     ",\"name\":" + json_quote(b.name) + "}";
        for (const Rate &r : kRates) {
            std::string bands_of;
            for (int b : r.bands) bands_of += (bands_of.empty() ? "" : ",") + std::to_string(b);
            rates += std::string(rates.empty() ? "" : ",") + "{\"label\":" + json_quote(r.label) +
                     ",\"sample_rate\":" + json_number(r.sample_rate) + ",\"capture_rate\":" +
                     json_number(r.capture_rate) + ",\"bands\":[" + bands_of + "]}";
        }
        for (unsigned w : kFilters) filters += (filters.empty() ? "" : ",") + std::to_string(w);
        const SpectrumSettings fft;  // what a server starts with
        const std::string fft_defaults =
            "{\"size\":" + std::to_string(fft.size) + ",\"window\":" + json_quote(fft.window) + ",\"rate\":" +
            json_number(fft.rate) + ",\"averaging\":" + std::to_string(fft.averaging) + ",\"peak\":" +
            (fft.peak ? "true" : "false") + ",\"dc\":" + (fft.remove_dc ? "true" : "false") + "}";
        web_.send_text(client, "{\"t\":\"hello\",\"version\":" + std::to_string(kProtocolVersion) +
                                   ",\"client\":" + std::to_string(client) + ",\"windows\":[" + windows +
                                   "],\"fft_defaults\":" + fft_defaults + ",\"max_bins\":" + std::to_string(kMaxBins) +
                                   ",\"controller\":" + (controller_ ? "true" : "false") + ",\"bands\":[" + bands +
                                   "],\"rates\":[" + rates + "],\"filters\":[" + filters + "],\"max_lna\":" + std::to_string(kMaxLna) +
                                   ",\"max_lpf\":" + std::to_string(kMaxLpf) + "}");
        send_status(client, viewer_, status_sections());
    }

    void on_close(unsigned client)
    {
        if (client == client_) client_ = 0;  // otherwise one that was replaced
    }

    void on_text(unsigned client, const std::string &message)
    {
        if (client != client_) return;
        Viewer &v = viewer_;
        try {
            const JsonValue m = parse_json(message);
            if (m.kind != JsonValue::Object) throw std::runtime_error("bad message: not an object");
            const std::string type = m.string_or("t");
            if (type == "ack") {
                acknowledge(client, v, uint32_t(m.number_or("seq", 0)));
            } else if (type == "ping") {
                web_.send_text(client, "{\"t\":\"pong\",\"c\":" + json_number(m.number_or("c", 0)) +
                                           ",\"s\":" + json_number(wall_ms()) + "}");
            } else if (type == "view") {
                v.start_hz = m.number_or("start", 0);
                v.stop_hz = m.number_or("stop", 0);
                v.bins = unsigned(std::clamp(m.number_or("bins", 1024), 16.0, double(kMaxBins)));
                v.auto_bins = m.boolean_or("auto", true);
                if (!v.auto_bins) v.bins_cap = 0;
                v.max_fps = std::clamp(m.number_or("fps", 0), 0.0, 100.0);
                v.max_kbps = std::clamp(m.number_or("kbps", 0), 0.0, 1e6);
                try_send(client, v);
            } else if (type == "set") {
                receiver_set(m);
            } else if (type == "run") {
                receiver_run(m.boolean_or("on", !streaming_), unsigned(m.number_or("channels", 1)));
            } else if (type == "fft") {
                SpectrumSettings s = engine_.settings();
                s.size = unsigned(m.number_or("size", s.size));
                if (std::string w = m.string_or("window"); !w.empty()) s.window = w;
                s.rate = m.number_or("rate", s.rate);
                s.averaging = unsigned(std::max(0.0, m.number_or("averaging", s.averaging)));
                s.peak = m.boolean_or("peak", s.peak);
                s.remove_dc = m.boolean_or("dc", s.remove_dc);
                const unsigned shown = s.channel;
                s.channel = unsigned(std::max(0.0, m.number_or("channel", s.channel)));
                s.validate();
                if (s.channel != shown) {  // what waited is of the channels shown before
                    for (unsigned c = 0; c < 2; ++c) {
                        v.pending[c].reset();
                        v.merged[c].reset();
                    }
                }
                engine_.configure(s);
                status_due_ = true;
            } else {
                throw std::runtime_error("unknown message type " + type);
            }
        } catch (const std::exception &e) {
            notice("error", e.what());
        }
        flush_status();
    }

    void on_wake()
    {
        if (controller_) controller_->poll();
        std::deque<std::shared_ptr<const SpectrumFrame>> frames;
        {
            std::lock_guard<std::mutex> lock(frames_mutex_);
            frames.swap(frames_);
        }
        if (client_)
            for (const auto &frame : frames) offer(client_, viewer_, frame);
        flush_status();
    }

    void on_tick()
    {
        stream_.refresh();
        // Off the CPU that takes the stream's packets, as the stream service is (placement.h).
        if (Clock::now() - placed_at_ >= std::chrono::seconds(2)) {
            placed_at_ = Clock::now();
            const int cpu = stream_.alive() ? stream_.receive_cpu() : -1;
            if (cpu != kept_off_) {
                placement::keep_off(cpu);
                kept_off_ = cpu;
            }
        }
        if (controller_) controller_->poll();
        if (dump_requested.exchange(false)) {
            if (dumping_) {
                std::fprintf(stderr, "dump: the previous one is still being written\n");
            } else {
                if (dump_thread_.joinable()) dump_thread_.join();  // finished: returns at once
                dumping_ = true;
                dump_thread_ = std::thread([this] {
                    dump();
                    dumping_ = false;
                });
            }
        }
        flush_status();
        if (!client_) return;
        auto now = Clock::now();
        Viewer &v = viewer_;
        // A client that stops acknowledging (a bug, or a frozen page) is not waited for
        // forever; a merely slow link acknowledges well within this.
        while (!v.in_flight.empty() && now - v.in_flight.front().time > 30s) {
            v.in_flight_bytes -= v.in_flight.front().bytes;
            v.in_flight.pop_front();
        }
        try_send(client_, v);
        double interval = std::chrono::duration<double>(now - v.mark).count();
        if (interval >= 1) {
            uint64_t bytes = web_.sent(client_);
            const double per_step = shows_both() ? 2 : 1;  // a pair of frames is one step of the display
            v.fps = v.frames_sent / per_step / interval;
            v.delivered_rate = (v.delivered - v.delivered_mark) / interval;
            v.delivered_mark = v.delivered;
            v.merged_rate = v.frames_merged / per_step / interval;
            v.kbps = (bytes - v.bytes_mark) * 8 / 1000.0 / interval;
            v.frames_sent = v.frames_merged = 0;
            v.bytes_mark = bytes;
            v.mark = now;
            adapt_resolution(v);
        }
        // Status takes at most about a fifth of what the link is seen to deliver (every
        // second when no frames flow), and waits while the link is backed up.
        auto status_interval = std::chrono::duration<double>(
            v.delivered_rate > 0 ? std::clamp(4.0 * v.last_status_bytes / v.delivered_rate, 0.25, 4.0) : 1.0);
        if (now - v.last_status >= status_interval && web_.queued(client_) < kSocketBacklog)
            send_status(client_, v, status_sections());
    }

    // In automatic resolution, halves or doubles the bins per frame so that the link carries
    // about 15 frames a second (fewer when fewer are made), judged only by the frames it really
    // delivered at the present size.
    void adapt_resolution(Viewer &v)
    {
        if (!v.auto_bins) {
            v.bins_cap = 0;
            return;
        }
        double target = std::min(engine_.settings().rate, 15.0);
        if (v.max_fps > 0) target = std::min(target, v.max_fps);
        unsigned cap = v.bins_cap ? v.bins_cap : v.bins;
        if (v.merged_rate > 0 && v.fps < 0.7 * target && cap > 256) cap /= 2;
        else if (v.fps > 1.6 * target && cap < v.bins) cap = std::min(v.bins, cap * 2);
        v.bins_cap = cap >= v.bins ? 0 : cap;
    }

    // Merges a new frame into what the viewer has waiting, then tries to send.
    void offer(unsigned client, Viewer &v, const std::shared_ptr<const SpectrumFrame> &frame)
    {
        const unsigned c = frame->channel & 1;      // a channel's frames merge with that channel's alone
        const SpectrumFrame *p = v.pending[c].get();
        bool same = p && p->run == frame->run && p->center_hz == frame->center_hz &&
                    p->sample_rate == frame->sample_rate && p->power.size() == frame->power.size() &&
                    p->peak == frame->peak;
        if (!same) {
            v.pending[c] = frame;
            v.merged[c].reset();
            v.pending_frames[c] = 1;
        } else {
            if (!v.merged[c]) {
                v.merged[c] = std::make_shared<SpectrumFrame>(*v.pending[c]);
                v.pending[c] = v.merged[c];
            }
            // The maximum, or the mean power, of the frames merged.
            const size_t bins = v.merged[c]->power.size();
            float *merged_power = v.merged[c]->power.data();
            const float *power = frame->power.data();
            if (frame->peak) {
                for (size_t i = 0; i < bins; ++i) merged_power[i] = std::max(merged_power[i], power[i]);
            } else {
                const float keep = float(v.pending_frames[c]) / float(v.pending_frames[c] + 1), add = 1 - keep;
                for (size_t i = 0; i < bins; ++i) merged_power[i] = merged_power[i] * keep + power[i] * add;
            }
            SpectrumFrame *merged = v.merged[c].get();
            merged->blocks += frame->blocks;
            merged->index = frame->index;
            ++v.pending_frames[c];
            ++v.frames_merged;
        }
        // With both channels shown, the pair goes together: channel 0's frame waits for channel
        // 1's over the same samples, which the engine gives next.
        if (shows_both() && !(v.pending[0] && v.pending[1])) return;
        try_send(client, v);
    }

    uint64_t window_bytes(const Viewer &v, size_t frame) const
    {
        return v.probing ? 1 : std::max<uint64_t>(frame, uint64_t(v.window));
    }

    void charge(unsigned client, Viewer &v, uint64_t before)
    {
        v.tokens -= double(web_.sent(client) + web_.queued(client) - before);
    }

    void try_send(unsigned client, Viewer &v)
    {
        if (!(v.pending[0] || v.pending[1]) || web_.queued(client) > kSocketBacklog || v.in_flight.size() >= 32) return;
        auto now = Clock::now();
        if (v.max_fps > 0 && now - v.last_send < std::chrono::duration<double>(1 / v.max_fps)) return;
        if (v.max_kbps > 0) {
            double budget = v.max_kbps * 1000 / 8;
            v.tokens = std::min(budget / 4, v.tokens + budget * std::chrono::duration<double>(now - v.tokens_at).count());
            v.tokens_at = now;
            if (v.tokens < 0) return;
        }
        if (!v.probing && now - v.rtt_min_at > 10s) v.probing = true;
        uint64_t estimate = v.last_frame_bytes ? v.last_frame_bytes : 64;
        if (!v.in_flight.empty() && v.in_flight_bytes + estimate > window_bytes(v, estimate)) return;

        // What waits goes now: one frame, or channel 0's and channel 1's, each acknowledged by itself.
        for (unsigned c = 0; c < 2; ++c) {
            if (!v.pending[c]) continue;
            std::string message = encode(*v.pending[c], v, v.sequence + 1, v.pending_frames[c]);
            uint64_t before = web_.sent(client) + web_.queued(client);
            web_.send_binary(client, message);
            uint64_t bytes = web_.sent(client) + web_.queued(client) - before;
            v.last_frame_bytes = bytes;
            charge(client, v, before);
            if (!v.window) v.window = double(bytes);
            v.in_flight.push_back({++v.sequence, now, bytes, v.in_flight.empty()});
            v.in_flight_bytes += bytes;
            v.pending[c].reset();
            v.merged[c].reset();
            ++v.frames_sent;
        }
        v.last_send = now;
    }

    void acknowledge(unsigned client, Viewer &v, uint32_t sequence)
    {
        auto now = Clock::now();
        while (!v.in_flight.empty() && int32_t(sequence - v.in_flight.front().sequence) >= 0) {
            Viewer::Sent sent = v.in_flight.front();
            v.in_flight.pop_front();
            v.in_flight_bytes -= sent.bytes;
            v.delivered += sent.bytes;
            if (sent.sequence != sequence) continue;
            double rtt = std::chrono::duration<double>(now - sent.time).count();
            v.rtt = v.rtt ? 0.8 * v.rtt + 0.2 * rtt : rtt;
            if (!v.rtt_min || rtt < v.rtt_min || (v.probing && sent.alone)) {
                v.rtt_min = rtt;
                v.rtt_min_at = now;
                v.probing = false;
            }
            double queueing = rtt - v.rtt_min, target = std::max(0.03, 0.25 * v.rtt_min);
            if (queueing > target) {
                if (now - v.cut_at > std::chrono::duration<double>(rtt)) {
                    v.window = std::max(double(sent.bytes), 0.7 * v.window);
                    v.cut_at = now;
                }
            } else if (queueing < target / 2) {
                v.window += double(sent.bytes);
            } else {
                v.window += double(sent.bytes) * double(sent.bytes) / v.window;
            }
            v.window = std::min(v.window, 4e6);
        }
        try_send(client, v);
    }

    // The frame fitted to the viewer's range and resolution: at most v.bins bins, each the
    // maximum of the FFT bins it covers (taken as power: one logarithm per bin sent), as 8-bit
    // steps of kStepDb above a base level.
    static std::string encode(const SpectrumFrame &f, const Viewer &v, uint32_t sequence, unsigned merged)
    {
        const size_t n = f.power.size();
        const double width = f.sample_rate / double(n), left = f.center_hz - f.sample_rate / 2 - width / 2;
        size_t first = 0, last = n;
        if (v.stop_hz > v.start_hz) {
            first = size_t(std::clamp(std::floor((v.start_hz - left) / width), 0.0, double(n - 1)));
            last = size_t(std::clamp(std::ceil((v.stop_hz - left) / width), double(first + 1), double(n)));
        }
        const size_t span = last - first, bins = std::min<size_t>(span, v.bins_cap ? std::min(v.bins_cap, v.bins) : v.bins);
        std::vector<float> values(bins);
        float low = 1e9f, high = -1e9f;
        for (size_t o = 0; o < bins; ++o) {
            size_t from = first + o * span / bins, to = std::max(from + 1, first + (o + 1) * span / bins);
            float peak = f.power[from];
            for (size_t i = from + 1; i < to; ++i) peak = std::max(peak, f.power[i]);
            peak = spectrum_db(peak);
            values[o] = peak;
            low = std::min(low, peak);
            high = std::max(high, peak);
        }
        float base = std::max(std::floor(low / kStepDb) * kStepDb, std::ceil(high / kStepDb) * kStepDb - 255 * kStepDb);

        std::string out(48 + bins, '\0');
        auto put = [&](size_t at, const void *data, size_t size) { std::memcpy(&out[at], data, size); };
        // flags: bit 0 the detector (peak), bit 1 the channel the frame is of.
        uint8_t kind = 1, flags = uint8_t((f.peak ? 1 : 0) | (f.channel & 1) << 1);
        uint16_t count = uint16_t(bins);
        double start = left + double(first) * width, stop = left + double(last) * width, time = wall_ms();
        float step = kStepDb;
        uint32_t frames = merged, blocks = f.blocks;
        put(0, &kind, 1);
        put(1, &flags, 1);
        put(2, &count, 2);
        put(4, &sequence, 4);
        put(8, &start, 8);
        put(16, &stop, 8);
        put(24, &base, 4);
        put(28, &step, 4);
        put(32, &frames, 4);
        put(36, &blocks, 4);
        put(40, &time, 8);
        for (size_t o = 0; o < bins; ++o)
            out[48 + o] = char(uint8_t(std::clamp(std::lround((values[o] - base) / kStepDb), 0L, 255L)));
        return out;
    }

    void flush_status()
    {
        if (!status_due_) return;
        status_due_ = false;
        if (client_ && web_.queued(client_) < kSocketBacklog) send_status(client_, viewer_, status_sections());
    }

    // Sends the status, leaving out the sections that rarely change when they are as last
    // sent (all of them go again every 10 s).
    void send_status(unsigned client, Viewer &v, const std::vector<std::pair<std::string, std::string>> &sections)
    {
        static const std::set<std::string> steady = {"devices", "receiver", "controller", "fft"};
        auto now = Clock::now();
        bool everything = now - v.sections_at > 10s;
        if (everything) v.sections_at = now;
        std::string text = "{\"t\":\"status\"";
        for (const auto &[name, json] : sections) {
            if (steady.count(name) && !everything && v.sections[name] == json) continue;
            v.sections[name] = json;
            text += ",\"" + name + "\":" + json;
        }
        v.last_status = now;
        char link[480];
        std::snprintf(link, sizeof(link),
                      ",\"link\":{\"rtt\":%.4f,\"rtt_min\":%.4f,\"delivered\":%.0f,\"window\":%llu,"
                      "\"in_flight\":%zu,\"in_flight_bytes\":%llu,\"fps\":%.1f,\"merged\":%.1f,\"kbps\":%.1f,"
                      "\"queued\":%zu,\"compressed\":%s,\"bins_cap\":%u,\"peer\":%s}}",
                      v.rtt, v.rtt_min, v.delivered_rate * 8 / 1000, (unsigned long long)window_bytes(v, 0),
                      v.in_flight.size(), (unsigned long long)v.in_flight_bytes, v.fps, v.merged_rate, v.kbps,
                      web_.queued(client), web_.compressing(client) ? "true" : "false", v.bins_cap,
                      json_quote(web_.peer(client)).c_str());
        text += link;
        uint64_t before = web_.sent(client) + web_.queued(client);
        web_.send_text(client, text);
        v.last_status_bytes = web_.sent(client) + web_.queued(client) - before;
        charge(client, v, before);
    }

    std::string waiting_message() const
    {
        if (!stream_.alive())
            return "The stream service is not running: start w1700k-stream (shared memory " + stream_.name() + ").";
        const std::string source = "the stream service";
        if (!controller_) return "No stream from " + source + ".";
        if (!controller_->alive()) return "No stream from " + source + ", and the controller is not running.";
        if (controller_->busy()) return "Waiting for the receiver: " + controller_->busy_text() + ".";
        if (streaming_) return "The receiver is streaming, but nothing arrives at " + source + ".";
        return "No stream. Press ▶ to start one.";
    }

    // The status as named JSON sections.
    std::vector<std::pair<std::string, std::string>> status_sections()
    {
        SpectrumSettings fft = engine_.settings();
        SpectrumEngine::Stats spectrum = engine_.stats();
        StreamClient::Stats in = stream_.stats();
        auto now = Clock::now();
        if (!marked_) {  // rates from here on, not from before the engine was ready
            mark_ = now;
            mark_spectrum_ = spectrum;
            mark_in_ = in;
            marked_ = true;
        }
        double interval = std::chrono::duration<double>(now - mark_).count();
        if (interval >= 1) {
            spectrum_fps_ = (spectrum.frames - mark_spectrum_.frames) / interval;
            spectrum_used_ = (spectrum.pairs_used - mark_spectrum_.pairs_used) / interval;
            spectrum_dropped_ = (spectrum.pairs_dropped - mark_spectrum_.pairs_dropped) / interval;
            byte_rate_ = (in.bytes - mark_in_.bytes) / interval;
            datagram_rate_ = (in.datagrams - mark_in_.datagrams) / interval;
            // a channel's samples a second: what the frames of the channel shown are made from
            sample_rate_ = (in.slices - mark_in_.slices) * double(kSliceSamples) / stream_.position().channels / interval;
            packed_share_ = in.slices > mark_in_.slices
                                ? double(in.packed - mark_in_.packed) / double(in.slices - mark_in_.slices) : 0;
            mark_ = now;
            mark_spectrum_ = spectrum;
            mark_in_ = in;
        }
        const bool live = in.last_any && steady_s() - in.last_any < 1;
        const double run_seconds = in.run ? in.last_any - in.run_started : 0;
        const Tuning tuning = engine_.tuning();

        std::vector<std::pair<std::string, std::string>> sections = {
            {"state", json_quote(live ? "running" : "waiting")},
            {"message", json_quote(live ? "" : waiting_message())},
            {"running", live ? "true" : "false"},
            {"time", json_number(wall_ms())},
            {"devices", "{\"source\":" + json_quote(stream_.name()) +
                            ",\"controller\":" + (controller_ ? json_quote(options_.controller) : "null") + "}"},
            {"receiver", "{\"lo\":" + json_number(tuning.center_hz) + ",\"sample_rate\":" +
                             json_number(tuning.sample_rate) + ",\"flatten\":" +
                             (stream_.flatten() ? "true" : "false") + ",\"flatten_available\":" +
                             (flatten_available() ? "true" : "false") + ",\"flatten_active\":" +
                             (in.flatten_active ? "true" : "false") + ",\"channels\":" +
                             std::to_string(stream_channels_) + "}"},
            {"controller", controller_ ? controller_->status_json() : "{\"mode\":\"off\"}"},
        };
        std::string s;
        auto section = [&](const char *name) {
            sections.push_back({name, s});
            s.clear();
        };
        s += "{\"size\":" + std::to_string(fft.size) + ",\"window\":" + json_quote(fft.window) +
             ",\"rate\":" + json_number(fft.rate) + ",\"averaging\":" + std::to_string(fft.averaging) +
             ",\"peak\":" + (fft.peak ? "true" : "false") + ",\"dc\":" + (fft.remove_dc ? "true" : "false") +
             ",\"channel\":" + std::to_string(fft.channel) + "}";
        section("fft");
        s += "{\"fps\":" + json_number(std::round(spectrum_fps_ * 10) / 10) +
             ",\"pairs_used\":" + json_number(std::round(spectrum_used_)) +
             ",\"pairs_dropped\":" + json_number(std::round(spectrum_dropped_)) +
             ",\"coverage\":" + json_number(sample_rate_ > 0 ? std::min(1.0, spectrum_used_ / sample_rate_ / (shows_both() ? 2 : 1)) : 0) +
             ",\"share\":" + json_number(std::round(spectrum.share * 1000) / 1000) +
             ",\"workers\":" + std::to_string(spectrum.workers) + ",\"queued\":" + std::to_string(spectrum.queued) +
             ",\"backlog\":" + std::to_string(spectrum.backlog) + ",\"missing\":" + std::to_string(spectrum.pairs_missing) +
             ",\"retuned\":" + std::to_string(spectrum.frames_retuned) + "}";
        section("spectrum");
        s += "{\"run\":" + std::to_string(in.run) + ",\"seconds\":" + json_number(run_seconds) +
             ",\"datagrams\":" + std::to_string(in.datagrams) + ",\"bytes\":" + std::to_string(in.bytes) +
             ",\"slices\":" + std::to_string(in.slices) + ",\"packed\":" + std::to_string(in.packed) +
             ",\"events\":" + std::to_string(in.events) + ",\"pieces\":" + std::to_string(in.pieces) +
             ",\"bad\":" + std::to_string(in.bad) + ",\"late\":" + std::to_string(in.late) +
             ",\"before_start\":" + std::to_string(in.before_start) +
             ",\"duplicates\":" + std::to_string(in.duplicates) + ",\"socket_drops\":" + std::to_string(in.socket_drops) +
             ",\"lost\":" + std::to_string(in.lost) + ",\"overruns\":" + std::to_string(in.overruns) +
             ",\"spare_words\":" + std::to_string(in.spare_words) +
             ",\"stale_slices\":" + std::to_string(in.stale_slices) +
             ",\"stale_lines\":" + std::to_string(in.stale_lines) +
             ",\"flattened\":" + std::to_string(in.flattened) +
             ",\"saturated\":" + std::to_string(in.saturated) +
             ",\"skipped\":" + std::to_string(spectrum.slices_skipped) +
             ",\"byte_rate\":" + json_number(std::round(byte_rate_)) +
             ",\"datagram_rate\":" + json_number(std::round(datagram_rate_)) +
             ",\"sample_rate\":" + json_number(std::round(sample_rate_)) +
             ",\"packed_share\":" + json_number(std::round(packed_share_ * 1000) / 1000) +
             ",\"ring_slices\":" + std::to_string(in.ring_slices) + "}";
        section("stream");
        return sections;
    }

    const ServeOptions &options_;
    StreamClient stream_;
    WebServer web_;

    std::mutex frames_mutex_;
    std::deque<std::shared_ptr<const SpectrumFrame>> frames_;
    SpectrumEngine engine_;  // after the queue its workers fill: destroyed (and joined) before it

    // Web thread only.
    Tuning tuning_;                 // the stream's, as the controller last reported it
    bool streaming_ = false;        // the controller reports a stream running
    unsigned stream_channels_ = 1;  // of that stream: 2, chains 0 and 1 together
    double radio_rate_ = 0, radio_capture_ = 0, radio_filter_ = 0;  // the controller's, for the correction
    unsigned tuning_ops_ = 0;       // tune or configure requests under way
    bool status_due_ = false;       // something the status shows has changed
    unsigned client_ = 0;           // the WebSocket being served; 0: none
    Viewer viewer_;
    Clock::time_point mark_ = Clock::now();
    bool marked_ = false;
    SpectrumEngine::Stats mark_spectrum_;
    StreamClient::Stats mark_in_;
    double spectrum_fps_ = 0, spectrum_used_ = 0, spectrum_dropped_ = 0;
    double byte_rate_ = 0, datagram_rate_ = 0, sample_rate_ = 0, packed_share_ = 0;
    std::thread dump_thread_;
    std::atomic<bool> dumping_{false};
    Clock::time_point placed_at_{};
    int kept_off_ = -1;

    // Last: destroyed first, while the web server its reader wakes still exists.
    std::unique_ptr<Controller> controller_;
};

}  // namespace

int command_serve(const ServeOptions &options)
{
    std::signal(SIGPIPE, SIG_IGN);  // a controller that exits closes the pipe we write
    Service service(options);
    std::fprintf(stderr, "serving on http://%s/, stream from w1700k-stream %s, controller %s  (Ctrl-C to stop)\n",
                 service.address().c_str(), options.stream.c_str(),
                 options.controller.empty() ? "off" : ("`" + options.controller + "`").c_str());
    // Who is answered: under which names, and with or without the token.
    std::string names;
    for (const auto &name : service.web().names()) names += (names.empty() ? "" : ", ") + name;
    std::fprintf(stderr, "answers to %s%s\n", names.c_str(),
                 options.behind_proxy ? " (as a proxy's X-Forwarded-Host, on any port)" : "; --host NAME adds a name");
    const bool beyond = service.address().compare(0, 4, "127.") != 0 && service.address().compare(0, 5, "[::1]") != 0;
    if (!service.web().token().empty())
        std::fprintf(stderr, "open %s\n(the token is wanted with every request: whoever has this link has the receiver)\n",
                     service.web().link().c_str());
    else if (beyond)
        std::fprintf(stderr, "open %s\nNO TOKEN IS WANTED (--no-token): whoever reaches this port has the receiver\n",
                     service.web().link().c_str());
    stop_requested = false;
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGUSR1, on_dump);
    service.run(stop_requested);
    std::fprintf(stderr, "stopping\n");
    return 0;
}
