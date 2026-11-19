// SPDX-License-Identifier: MIT
// The W1700K receiver as a SoapySDR device ("driver=w1700k"): one receive channel. Its samples
// are the ones w1700k-stream serves to every consumer on this host (host/stream: shared memory);
// its settings go to the receiver's controller, `w1700k serve` (host/w1700k). See ../README.md.
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <dirent.h>
#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

#include <SoapySDR/Device.hpp>
#include <SoapySDR/Formats.hpp>
#include <SoapySDR/Logger.hpp>
#include <SoapySDR/Registry.hpp>

#include "control.h"
#include "ring.h"

namespace {

constexpr int kBands = 3;
const char *const kAntenna[kBands] = {"2.4G", "5G", "6G"};
// Each band's LO span until the unit reports its own (a load measures it): unit A's.
constexpr double kDefaultSpan[kBands][2] = {{2080.7e6, 2748.0e6}, {4646.6e6, 7509.0e6}, {4707.2e6, 7576.2e6}};
constexpr double kRates[] = {40e6, 80e6, 160e6, 320e6};
constexpr double kFilters[] = {20e6, 40e6, 80e6, 160e6, 320e6};
// Gain: the LNA's 5 steps of about 6 dB and the baseband filter's 10 of about 2 dB, as measured
// (include/hw/adie/adie.h).
constexpr double kLnaStep = 6, kLpfStep = 2;
constexpr int kLnaMax = 4, kLpfMax = 9;
constexpr size_t kMtu = 8 * W1700K_STREAM_SLICE;

std::string arg_or(const SoapySDR::Kwargs &args, const char *key, const std::string &fallback)
{
    const auto it = args.find(key);
    return it == args.end() ? fallback : it->second;
}

std::string env_or(const char *name, const std::string &fallback)
{
    const char *value = std::getenv(name);
    return value && *value ? value : fallback;
}

// A device's arguments are text from whoever asked for the device, and that is not always the
// user who runs the application: SoapyRemote's server makes a device from the arguments of any
// client that connects. So no argument is a command, and none is put into one unchecked.
//
// The two commands this module runs (the controller, and a stream service to start) come from
// the environment of the process that loaded it. As an argument only "none" is taken, which
// runs nothing.
std::string command_or_none(const SoapySDR::Kwargs &args, const char *key, const char *variable, const std::string &fallback)
{
    const std::string given = arg_or(args, key, "");
    if (given == "none") return given;
    if (!given.empty())
        throw std::runtime_error(std::string("w1700k: ") + key + "= is not taken from a device's arguments (only " + key +
                                 "=none is): a command is the environment's to give, " + variable + "=COMMAND");
    return env_or(variable, fallback);
}

// The stream's name, as the service takes it: a slash and up to 64 of A-Z a-z 0-9 . _ -
std::string stream_name(const SoapySDR::Kwargs &args)
{
    const std::string name = arg_or(args, "stream", W1700K_STREAM_NAME);
    const bool good = name.size() >= 2 && name.size() <= 65 && name[0] == '/' &&
                      name.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-", 1) == std::string::npos;
    if (!good) throw std::runtime_error("w1700k: stream= names the stream service's shared memory, like " W1700K_STREAM_NAME);
    return name;
}

// A packing the controller knows (host/w1700k/config.py, PACKINGS), or none given.
std::string packing_of(const SoapySDR::Kwargs &args)
{
    const std::string packing = arg_or(args, "packing", "");
    static const char *const known[] = {"raw", "half", "full", "split", "16", "14", "8", "7"};
    if (packing.empty() || std::find(std::begin(known), std::end(known), packing) != std::end(known)) return packing;
    throw std::runtime_error("w1700k: packing= is one of raw, half, full, split, 16, 14, 8, 7");
}

bool on_path(const std::string &program)
{
    const std::string path = env_or("PATH", "");
    for (size_t from = 0; from <= path.size();) {
        const size_t to = std::min(path.find(':', from), path.size());
        if (to > from && access((path.substr(from, to - from) + "/" + program).c_str(), X_OK) == 0) return true;
        from = to + 1;
    }
    return false;
}

bool truthy(const std::string &value)
{
    return value == "true" || value == "1" || value == "on" || value == "yes";
}

std::string number(double value)
{
    char text[32];
    std::snprintf(text, sizeof(text), "%.0f", value);
    return text;
}

void log_from_controller(const std::string &level, const std::string &text)
{
    const SoapySDRLogLevel to = level == "error" ? SOAPY_SDR_ERROR : level == "warn" ? SOAPY_SDR_WARNING
                              : level == "debug" ? SOAPY_SDR_DEBUG : SOAPY_SDR_INFO;
    SoapySDR::logf(to, "w1700k: %s", text.c_str());
}

// The stream service, started here because none was running: it ends with the device.
class Service {
public:
    // `program` and its arguments, as they are: found on PATH, with no shell between.
    explicit Service(const std::vector<std::string> &program)
    {
        std::vector<char *> argv;
        for (const auto &word : program) argv.push_back(const_cast<char *>(word.c_str()));
        argv.push_back(nullptr);
        pid_ = fork();
        if (pid_ == 0) {
            setpgid(0, 0);
            const int null = open("/dev/null", O_RDONLY);
            if (null >= 0) dup2(null, STDIN_FILENO);
            dup2(STDERR_FILENO, STDOUT_FILENO);  // the application's stdout is its own
            execvp(argv[0], argv.data());
            _exit(127);
        }
        if (pid_ < 0) throw std::runtime_error(std::string("fork: ") + std::strerror(errno));
        setpgid(pid_, pid_);
    }
    ~Service()
    {
        int status;
        kill(-pid_, SIGINT);  // it unlinks its shared memory on the way out
        for (int k = 0; k < 150; k++) {
            const pid_t r = waitpid(pid_, &status, WNOHANG);
            if (r == pid_ || (r < 0 && errno == ECHILD)) return;
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        kill(-pid_, SIGKILL);
        waitpid(pid_, &status, 0);
    }
    bool exited()
    {
        int status;
        return waitpid(pid_, &status, WNOHANG) != 0;
    }

private:
    pid_t pid_ = -1;
};

// The controller's STATE, as far as a device needs it.
struct Radio {
    bool loaded = false, running = false, agc = true, follows = true;
    int band = 0, lna = -1, lpf = -1, channels = 1;   // channels: the running stream's, 1 or 2
    double lo_hz = 0, rate = 0, capture = 0, filter_mhz = 0, link_mbps = 0;
    std::string packing, error;
    int locked[kBands] = {-1, -1, -1};  // -1: not read
};

Radio radio_from(const JsonValue &state)
{
    Radio r;
    static const JsonValue none;
    const JsonValue *radio = state.find("radio"), *stream = state.find("stream"), *rf = state.find("rf");
    if (!radio) radio = &none;
    if (!stream) stream = &none;
    if (!rf) rf = &none;
    r.loaded = radio->boolean_or("loaded", false);
    r.band = std::clamp(int(radio->number_or("band", 0)), 0, kBands - 1);
    r.lo_hz = rf->number_or("lo_hz", radio->number_or("lo_hz", 0));
    r.rate = radio->number_or("sample_rate", 0);
    r.capture = radio->number_or("capture_rate", r.rate);
    r.running = stream->boolean_or("running", false);
    r.channels = stream->number_or("channels", 1) == 2 ? 2 : 1;
    r.packing = stream->string_or("packing");
    r.link_mbps = stream->number_or("link_mbps", 0);
    r.agc = rf->boolean_or("agc", true);
    r.lna = int(rf->number_or("lna", -1));
    r.lpf = int(rf->number_or("lpf", -1));
    r.filter_mhz = rf->number_or("filter_mhz", 0);
    r.follows = rf->boolean_or("filter_follows_rate", true);
    r.error = state.string_or("error");
    if (const JsonValue *locks = radio->find("synth_locked"))
        for (size_t b = 0; b < locks->items.size() && b < size_t(kBands); b++)
            if (locks->items[b].is_boolean()) r.locked[b] = locks->items[b].boolean;
    return r;
}

class W1700K : public SoapySDR::Device {
public:
    explicit W1700K(const SoapySDR::Kwargs &args);
    ~W1700K() override;

    std::string getDriverKey() const override { return "w1700k"; }
    std::string getHardwareKey() const override { return "W1700K"; }
    SoapySDR::Kwargs getHardwareInfo() const override;

    // Two receive channels: chains 0 and 1 of the band in use, sampled at the same instants. They
    // are one radio: frequency, sample rate, bandwidth and gain are common to both, and setting
    // one channel's sets the other's.
    size_t getNumChannels(const int direction) const override { return direction == SOAPY_SDR_RX ? 2 : 0; }
    bool getFullDuplex(const int, const size_t) const override { return false; }

    std::vector<std::string> getStreamFormats(const int, const size_t) const override
    {
        return {SOAPY_SDR_CS16, SOAPY_SDR_CF32};
    }
    std::string getNativeStreamFormat(const int, const size_t, double &full_scale) const override
    {
        full_scale = W1700K_STREAM_FULL_SCALE;  // the radio's 14-bit samples, as they are
        return SOAPY_SDR_CS16;
    }
    SoapySDR::Stream *setupStream(const int direction, const std::string &format, const std::vector<size_t> &channels,
                                  const SoapySDR::Kwargs &args) override;
    void closeStream(SoapySDR::Stream *stream) override;
    size_t getStreamMTU(SoapySDR::Stream *) const override { return kMtu; }
    int activateStream(SoapySDR::Stream *stream, const int flags, const long long time_ns, const size_t count) override;
    int deactivateStream(SoapySDR::Stream *stream, const int flags, const long long time_ns) override;
    int readStream(SoapySDR::Stream *stream, void *const *buffers, const size_t count, int &flags, long long &time_ns,
                   const long timeout_us) override;

    std::vector<std::string> listAntennas(const int, const size_t) const override
    {
        return {kAntenna[0], kAntenna[1], kAntenna[2]};
    }
    void setAntenna(const int direction, const size_t channel, const std::string &name) override;
    std::string getAntenna(const int, const size_t) const override { return kAntenna[radio().band]; }

    using SoapySDR::Device::getGain;
    using SoapySDR::Device::getGainRange;
    using SoapySDR::Device::setGain;
    std::vector<std::string> listGains(const int, const size_t) const override { return {"LNA", "LPF"}; }
    bool hasGainMode(const int, const size_t) const override { return true; }
    void setGainMode(const int direction, const size_t channel, const bool automatic) override;
    bool getGainMode(const int, const size_t) const override { return radio().agc; }
    void setGain(const int direction, const size_t channel, const std::string &name, const double value) override;
    double getGain(const int direction, const size_t channel, const std::string &name) const override;
    SoapySDR::Range getGainRange(const int direction, const size_t channel, const std::string &name) const override;

    using SoapySDR::Device::getFrequency;
    using SoapySDR::Device::getFrequencyRange;
    using SoapySDR::Device::setFrequency;
    void setFrequency(const int direction, const size_t channel, const std::string &name, const double frequency,
                      const SoapySDR::Kwargs &args) override;
    double getFrequency(const int, const size_t, const std::string &name) const override
    {
        return name == "RF" ? radio().lo_hz : 0;
    }
    std::vector<std::string> listFrequencies(const int, const size_t) const override { return {"RF"}; }
    SoapySDR::RangeList getFrequencyRange(const int direction, const size_t channel,
                                          const std::string &name) const override;

    void setSampleRate(const int direction, const size_t channel, const double rate) override;
    double getSampleRate(const int, const size_t) const override { return radio().rate; }
    std::vector<double> listSampleRates(const int, const size_t) const override
    {
        return {std::begin(kRates), std::end(kRates)};
    }
    SoapySDR::RangeList getSampleRateRange(const int, const size_t) const override
    {
        SoapySDR::RangeList ranges;
        for (double rate : kRates) ranges.emplace_back(rate, rate);
        return ranges;
    }

    void setBandwidth(const int direction, const size_t channel, const double bandwidth) override;
    double getBandwidth(const int, const size_t) const override { return radio().filter_mhz * 1e6; }
    std::vector<double> listBandwidths(const int, const size_t) const override
    {
        return {std::begin(kFilters), std::end(kFilters)};
    }
    SoapySDR::RangeList getBandwidthRange(const int, const size_t) const override
    {
        SoapySDR::RangeList ranges;
        for (double width : kFilters) ranges.emplace_back(width, width);
        return ranges;
    }

    std::vector<std::string> listSensors() const override;
    SoapySDR::ArgInfo getSensorInfo(const std::string &key) const override;
    std::string readSensor(const std::string &key) const override;

    SoapySDR::ArgInfoList getSettingInfo() const override;
    void writeSetting(const std::string &key, const std::string &value) override;
    std::string readSetting(const std::string &key) const override;

private:
    struct Rx {
        explicit Rx(Ring &ring) : reader(ring) {}
        Reader reader;
        bool cf32 = false, active = false;
        unsigned channels = 1;   // 2: channels 0 and 1 together
    };

    // The receiver as the controller last reported it (what has arrived is taken in first).
    Radio radio() const;
    // With mutex_ held: takes in the controller's state where it changed.
    void sync() const;
    // With mutex_ held: one request, then its state. Throws what the controller refused it with.
    void command(const std::string &op, const std::string &fields, double timeout_s);
    void need_controller() const;
    void need_loaded();  // with mutex_ held: loads the radio if nothing has yet
    void check_channel(const int direction, const size_t channel) const;
    int band_for(double hz) const;  // with mutex_ held; -1: no band tunes there
    std::string configure_fields(int band, double rate, double capture) const;

    const std::string stream_name_;
    std::string controller_command_;
    mutable std::mutex mutex_;
    mutable std::unique_ptr<Control> control_;
    mutable Ring ring_;
    std::unique_ptr<Service> service_;
    mutable Radio radio_;
    mutable uint64_t state_serial_ = 0;
    mutable double span_[kBands][2];
    mutable std::atomic<double> rate_{0};
    std::atomic<uint64_t> synced_ns_{0};   // when a reader last looked at the controller's state
    int manual_lna_ = 2, manual_lpf_ = 5;  // what a gain set by name starts from
    int active_ = 0;                       // streams reading
    bool started_here_ = false;            // the unit's stream was started by this device
};

W1700K::W1700K(const SoapySDR::Kwargs &args)
    : stream_name_(stream_name(args)), ring_(stream_name_)
{
    for (int b = 0; b < kBands; b++) std::copy(kDefaultSpan[b], kDefaultSpan[b] + 2, span_[b]);
    // Every argument is judged before anything is run for any of them.
    const std::string packing = packing_of(args);
    const std::string service_command = command_or_none(args, "service", "W1700K_STREAM_SERVICE", "");
    controller_command_ = command_or_none(args, "controller", "W1700K_CONTROLLER", "w1700k serve");

    // The samples: the stream service, started here if none runs and one can be.
    if (!ring_.alive()) {
        // The environment's command goes to a shell, as the user wrote it; our own is run as it
        // is, the stream's name an argument of its own.
        std::string service = service_command;
        std::vector<std::string> program;
        if (!service.empty() && service != "none") {
            program = {"/bin/sh", "-c", "exec " + service};
        } else if (service.empty() && on_path("w1700k-stream")) {
            program = {"w1700k-stream"};
            if (stream_name_ != W1700K_STREAM_NAME) program.insert(program.end(), {"--name", stream_name_});
            service = "w1700k-stream";
        }
        if (program.empty())
            throw std::runtime_error("no stream service at " + stream_name_ + ": start w1700k-stream (host/stream), or "
                                     "name the command that starts it in the environment, W1700K_STREAM_SERVICE");
        SoapySDR::logf(SOAPY_SDR_INFO, "w1700k: starting the stream service: %s", service.c_str());
        service_ = std::make_unique<Service>(program);
        for (int k = 0; k < 100 && !ring_.alive(); k++) {
            if (service_->exited()) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (!ring_.alive()) {
            service_.reset();
            throw std::runtime_error("the stream service `" + service + "` did not come up at " + stream_name_);
        }
    }

    // The settings: the receiver's controller, unless this device only listens.
    if (controller_command_ == "none") {
        controller_command_.clear();
        const auto view = ring_.view();
        rate_ = view ? double(__atomic_load_n(&view->header()->radio_sample_rate, __ATOMIC_ACQUIRE)) : 0;
        radio_.rate = rate_;
        return;
    }
    // The packing is a word of the controller's own, handed to it as an argument: never text
    // of the command a shell reads.
    std::vector<std::string> more;
    if (!packing.empty()) more = {"--packing", packing};
    control_ = std::make_unique<Control>(controller_command_, more, log_from_controller);
    std::lock_guard<std::mutex> lock(mutex_);
    sync();
}

W1700K::~W1700K()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (control_ && started_here_) {
        try {
            command("stop", "", 120);
        } catch (const std::exception &e) {
            SoapySDR::logf(SOAPY_SDR_WARNING, "w1700k: stopping the stream: %s", e.what());
        }
    }
    control_.reset();
}

void W1700K::sync() const
{
    if (!control_) return;
    const JsonValue &state = control_->state();
    if (control_->state_serial() == state_serial_) return;
    state_serial_ = control_->state_serial();
    radio_ = radio_from(state);
    if (const JsonValue *span = state.at("rf.lo_range_hz"))
        if (span->items.size() == 2 && span->items[0].is_number() && span->items[1].is_number()) {
            span_[radio_.band][0] = span->items[0].number;
            span_[radio_.band][1] = span->items[1].number;
        }
    rate_ = radio_.rate;
    // The service corrects the stream's roll-off by what it is told of the radio.
    if (radio_.loaded) ring_.set_radio(radio_.rate, radio_.capture, radio_.filter_mhz * 1e6);
    if (radio_.running) ring_.set_channels(unsigned(radio_.channels));
}

Radio W1700K::radio() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    sync();
    return radio_;
}

void W1700K::need_controller() const
{
    if (!control_) throw std::runtime_error("w1700k: this device only listens (controller=none)");
}

void W1700K::command(const std::string &op, const std::string &fields, double timeout_s)
{
    need_controller();
    try {
        control_->request(op, fields, timeout_s);
    } catch (...) {
        sync();
        throw;
    }
    sync();
}

void W1700K::need_loaded()
{
    need_controller();
    sync();
    if (radio_.loaded) return;
    SoapySDR::log(SOAPY_SDR_INFO, "w1700k: loading the radio (firmware, calibration): a few seconds");
    command("load", "", 600);
}

void W1700K::check_channel(const int direction, const size_t channel) const
{
    if (direction != SOAPY_SDR_RX || channel > 1) throw std::runtime_error("w1700k: receive channels 0 and 1");
}

SoapySDR::Kwargs W1700K::getHardwareInfo() const
{
    SoapySDR::Kwargs info;
    info["stream"] = stream_name_;
    info["controller"] = controller_command_.empty() ? "none" : controller_command_;
    info["origin"] = "https://github.com/h0m3us3r/w1700k-sdr";
    return info;
}

// --- stream ---

SoapySDR::Stream *W1700K::setupStream(const int direction, const std::string &format,
                                      const std::vector<size_t> &channels, const SoapySDR::Kwargs &)
{
    if (direction != SOAPY_SDR_RX) throw std::runtime_error("w1700k: receive only");
    // Channel 0 alone, or 0 and 1 together: channel 1 is the second of a pair, and the unit
    // streams it only beside channel 0.
    const bool two = channels.size() == 2 && channels[0] == 0 && channels[1] == 1;
    if (!two && (channels.size() > 1 || (channels.size() == 1 && channels[0] != 0)))
        throw std::runtime_error("w1700k: a stream of channel 0, or of channels 0 and 1 in that order");
    if (format != SOAPY_SDR_CS16 && format != SOAPY_SDR_CF32)
        throw std::runtime_error("w1700k: stream format " + format + ": CS16 or CF32");
    auto rx = std::make_unique<Rx>(ring_);
    rx->cf32 = format == SOAPY_SDR_CF32;
    rx->channels = two ? 2 : 1;
    rx->reader.set_channels(rx->channels);
    return reinterpret_cast<SoapySDR::Stream *>(rx.release());
}

void W1700K::closeStream(SoapySDR::Stream *stream)
{
    deactivateStream(stream, 0, 0);
    delete reinterpret_cast<Rx *>(stream);
}

int W1700K::activateStream(SoapySDR::Stream *stream, const int flags, const long long, const size_t)
{
    if (flags) return SOAPY_SDR_NOT_SUPPORTED;  // no timed starts or bursts: the stream runs
    Rx *rx = reinterpret_cast<Rx *>(stream);
    std::lock_guard<std::mutex> lock(mutex_);
    if (rx->active) return 0;
    if (control_) {
        need_loaded();
        if (!radio_.running) {
            command("start", rx->channels == 2 ? ",\"channels\":2" : "", 120);
            started_here_ = true;
        } else if (rx->channels == 2 && radio_.channels != 2) {
            // Not stopped for it: the stream running has readers of its own.
            throw std::runtime_error("w1700k: the stream that is running has one channel; stop it, and a stream of "
                                     "channels 0 and 1 starts one of two");
        }
    }
    rx->reader.start(rate_);
    rx->active = true;
    active_++;
    return 0;
}

int W1700K::deactivateStream(SoapySDR::Stream *stream, const int flags, const long long)
{
    if (flags) return SOAPY_SDR_NOT_SUPPORTED;
    Rx *rx = reinterpret_cast<Rx *>(stream);
    std::lock_guard<std::mutex> lock(mutex_);
    if (!rx->active) return 0;
    rx->active = false;
    const Reader::Tally &t = rx->reader.tally();
    SoapySDR::logf(t.lapped ? SOAPY_SDR_WARNING : SOAPY_SDR_INFO,
                   "w1700k: read %llu samples with %llu gaps, %llu of them because the reader was too slow; "
                   "at most %.0f ms behind the stream",
                   (unsigned long long)t.samples, (unsigned long long)t.gaps, (unsigned long long)t.lapped,
                   rate_ > 0 ? double(t.behind) * w1700k_stream_slice_samples(radio_.channels) * 1e3 / rate_ : 0.0);
    // The unit's stream stops with its last reader here, if it was started here: one that was
    // already running has other consumers on this host.
    if (--active_ == 0 && started_here_ && control_) {
        started_here_ = false;
        try {
            command("stop", "", 120);
        } catch (const std::exception &e) {
            SoapySDR::logf(SOAPY_SDR_ERROR, "w1700k: stopping the stream: %s", e.what());
            return SOAPY_SDR_STREAM_ERROR;
        }
    }
    return 0;
}

int W1700K::readStream(SoapySDR::Stream *stream, void *const *buffers, const size_t count, int &flags,
                       long long &time_ns, const long timeout_us)
{
    Rx *rx = reinterpret_cast<Rx *>(stream);
    if (!rx->active) return SOAPY_SDR_STREAM_ERROR;
    flags = 0;
    // A program that only reads asks for nothing, and the sample rate may be changed from
    // elsewhere (the page): look at what the controller has reported a few times a second,
    // unless a setter is at work.
    const uint64_t now = w1700k_stream_now_ns();
    if (now - synced_ns_.load(std::memory_order_relaxed) > 250000000ull) {
        synced_ns_.store(now, std::memory_order_relaxed);
        if (mutex_.try_lock()) {
            sync();
            mutex_.unlock();
        }
    }
    // One buffer a channel, each of `count` samples; the same sample index is the same instant.
    const long n = rx->reader.read(rx->cf32 ? nullptr : reinterpret_cast<int16_t *const *>(buffers),
                                   rx->cf32 ? reinterpret_cast<float *const *>(buffers) : nullptr, count, timeout_us,
                                   rate_.load(std::memory_order_relaxed), time_ns);
    if (n == Reader::kTimeout) return SOAPY_SDR_TIMEOUT;
    flags = SOAPY_SDR_HAS_TIME;
    if (n == Reader::kGap) return SOAPY_SDR_OVERFLOW;  // time_ns: where the stream goes on
    return int(n);
}

// --- bands and frequency ---

int W1700K::band_for(double hz) const
{
    const auto holds = [&](int band) { return hz >= span_[band][0] && hz <= span_[band][1]; };
    if (holds(radio_.band)) return radio_.band;  // the band in use, as long as it tunes there
    if (holds(0)) return 0;
    // The 5 GHz and 6 GHz radios overlap: each for its own band, the other where only it reaches.
    const int first = hz < 5925e6 ? 1 : 2;
    return holds(first) ? first : holds(3 - first) ? 3 - first : -1;
}

std::string W1700K::configure_fields(int band, double rate, double capture) const
{
    return ",\"band\":" + std::to_string(band) + ",\"sample_rate\":" + number(rate) +
           ",\"capture_rate\":" + number(capture);
}

void W1700K::setAntenna(const int direction, const size_t channel, const std::string &name)
{
    check_channel(direction, channel);
    const auto found = std::find(std::begin(kAntenna), std::end(kAntenna), name);
    if (found == std::end(kAntenna)) throw std::runtime_error("w1700k: antenna " + name + ": 2.4G, 5G or 6G");
    const int band = int(found - std::begin(kAntenna));
    std::lock_guard<std::mutex> lock(mutex_);
    need_loaded();
    if (band == radio_.band) return;
    // The sample rate is the device's here, not each band's: it goes with the switch.
    command("configure", configure_fields(band, radio_.rate, radio_.rate), 600);
}

void W1700K::setFrequency(const int direction, const size_t channel, const std::string &name,
                          const double frequency, const SoapySDR::Kwargs &)
{
    check_channel(direction, channel);
    if (name != "RF") throw std::runtime_error("w1700k: frequency component " + name + ": RF");
    std::lock_guard<std::mutex> lock(mutex_);
    need_loaded();
    const int band = band_for(frequency);
    if (band < 0) {
        char text[160];
        std::snprintf(text, sizeof(text), "w1700k: %.3f MHz is outside every band (%.1f-%.1f, %.1f-%.1f MHz)",
                      frequency / 1e6, span_[0][0] / 1e6, span_[0][1] / 1e6,
                      std::min(span_[1][0], span_[2][0]) / 1e6, std::max(span_[1][1], span_[2][1]) / 1e6);
        throw std::runtime_error(text);
    }
    if (band == radio_.band)
        command("tune", ",\"hz\":" + number(frequency), 30);
    else
        command("configure", configure_fields(band, radio_.rate, radio_.rate) + ",\"lo_hz\":" + number(frequency), 600);
}

SoapySDR::RangeList W1700K::getFrequencyRange(const int, const size_t, const std::string &name) const
{
    if (name != "RF") return {};
    std::lock_guard<std::mutex> lock(mutex_);
    sync();
    return {SoapySDR::Range(span_[0][0], span_[0][1]),
            SoapySDR::Range(std::min(span_[1][0], span_[2][0]), std::max(span_[1][1], span_[2][1]))};
}

// --- sample rate and filter ---

void W1700K::setSampleRate(const int direction, const size_t channel, const double rate)
{
    check_channel(direction, channel);
    if (std::find(std::begin(kRates), std::end(kRates), rate) == std::end(kRates))
        throw std::runtime_error("w1700k: sample rate " + number(rate) + ": 40, 80, 160 or 320 Msps");
    std::lock_guard<std::mutex> lock(mutex_);
    need_loaded();
    if (rate == radio_.rate && radio_.capture == radio_.rate) return;
    command("configure", configure_fields(radio_.band, rate, rate), 600);
}

void W1700K::setBandwidth(const int direction, const size_t channel, const double bandwidth)
{
    check_channel(direction, channel);
    std::lock_guard<std::mutex> lock(mutex_);
    need_loaded();
    if (bandwidth <= 0) {  // as wide as the sample rate, and following it
        command("filter_follow", ",\"on\":true", 30);
        return;
    }
    // The narrowest filter that passes what was asked for.
    const double *width = std::lower_bound(std::begin(kFilters), std::end(kFilters), bandwidth);
    if (width == std::end(kFilters)) width--;
    command("filter", ",\"mhz\":" + number(*width / 1e6), 30);
}

// --- gain ---

void W1700K::setGainMode(const int direction, const size_t channel, const bool automatic)
{
    check_channel(direction, channel);
    std::lock_guard<std::mutex> lock(mutex_);
    need_loaded();
    if (automatic)
        command("gain", ",\"agc\":true", 30);
    else
        command("gain", ",\"lna\":" + std::to_string(manual_lna_) + ",\"lpf\":" + std::to_string(manual_lpf_), 30);
}

void W1700K::setGain(const int direction, const size_t channel, const std::string &name, const double value)
{
    check_channel(direction, channel);
    std::lock_guard<std::mutex> lock(mutex_);
    need_loaded();
    if (!radio_.agc && radio_.lna >= 0 && radio_.lpf >= 0) {
        manual_lna_ = radio_.lna;
        manual_lpf_ = radio_.lpf;
    }
    if (name == "LNA")
        manual_lna_ = std::clamp(int(std::lround(value / kLnaStep)), 0, kLnaMax);
    else if (name == "LPF")
        manual_lpf_ = std::clamp(int(std::lround(value / kLpfStep)), 0, kLpfMax);
    else
        throw std::runtime_error("w1700k: gain " + name + ": LNA or LPF");
    // A gain set by hand ends the automatic gain: the radio has one or the other.
    command("gain", ",\"lna\":" + std::to_string(manual_lna_) + ",\"lpf\":" + std::to_string(manual_lpf_), 30);
}

double W1700K::getGain(const int, const size_t, const std::string &name) const
{
    const Radio r = radio();
    const bool manual = !r.agc && r.lna >= 0 && r.lpf >= 0;
    if (name == "LNA") return kLnaStep * (manual ? r.lna : manual_lna_);
    if (name == "LPF") return kLpfStep * (manual ? r.lpf : manual_lpf_);
    return 0;
}

SoapySDR::Range W1700K::getGainRange(const int, const size_t, const std::string &name) const
{
    if (name == "LNA") return SoapySDR::Range(0, kLnaStep * kLnaMax, kLnaStep);
    if (name == "LPF") return SoapySDR::Range(0, kLpfStep * kLpfMax, kLpfStep);
    return SoapySDR::Range();
}

// --- sensors and settings ---

namespace {

struct Counter {
    const char *key, *name, *description;
    uint64_t w1700k_stream_header::*field;
};
const Counter kCounters[] = {
    {"lost", "Lost slices", "slices of 2048 samples that never arrived, since the stream service started",
     &w1700k_stream_header::lost},
    {"late", "Late slices", "slices that arrived too late to be placed", &w1700k_stream_header::late},
    {"overruns", "Overruns", "of the lost: arrived, but the service fell behind", &w1700k_stream_header::overruns},
    {"reduced", "Reduced slices", "slices sent with fewer than 14 bits per component",
     &w1700k_stream_header::reduced},
    {"slices", "Slices", "slices of samples received", &w1700k_stream_header::slices},
};

SoapySDR::ArgInfo info(const std::string &key, const std::string &name, const std::string &description,
                       SoapySDR::ArgInfo::Type type, const std::string &value = "")
{
    SoapySDR::ArgInfo a;
    a.key = key;
    a.name = name;
    a.description = description;
    a.type = type;
    a.value = value;
    return a;
}

}  // namespace

std::vector<std::string> W1700K::listSensors() const
{
    std::vector<std::string> keys = {"streaming", "packing", "link_mbps", "synth_locked", "error", "service"};
    for (const Counter &c : kCounters) keys.push_back(c.key);
    return keys;
}

SoapySDR::ArgInfo W1700K::getSensorInfo(const std::string &key) const
{
    using T = SoapySDR::ArgInfo;
    if (key == "streaming") return info(key, "Streaming", "the unit is sending its stream", T::BOOL);
    if (key == "packing")
        return info(key, "Packing", "how the stream fits the link: raw, half, full (lossless); "
                                    "split, 16, 14, 8, 7 (fewer bits per sample)", T::STRING);
    if (key == "link_mbps") return info(key, "Link speed", "the stream link's speed, Mbit/s", T::INT);
    if (key == "synth_locked") return info(key, "Synthesizer locked", "the captured band's LO is in lock", T::BOOL);
    if (key == "error") return info(key, "Receiver error", "what is wrong with the receiver, or nothing", T::STRING);
    if (key == "service") return info(key, "Stream service", "w1700k-stream is running on this host", T::BOOL);
    for (const Counter &c : kCounters)
        if (key == c.key) return info(key, c.name, c.description, T::INT);
    return T();
}

std::string W1700K::readSensor(const std::string &key) const
{
    if (key == "service") return ring_.alive() ? "true" : "false";
    for (const Counter &c : kCounters)
        if (key == c.key) {
            const auto view = ring_.view();
            return std::to_string(view ? __atomic_load_n(&(view->header()->*c.field), __ATOMIC_ACQUIRE) : 0);
        }
    const Radio r = radio();
    if (key == "streaming") return r.running ? "true" : "false";
    if (key == "packing") return r.packing;
    if (key == "link_mbps") return number(r.link_mbps);
    if (key == "synth_locked") return r.locked[r.band] < 0 ? "" : r.locked[r.band] ? "true" : "false";
    if (key == "error") return r.error;
    throw std::runtime_error("w1700k: no sensor " + key);
}

SoapySDR::ArgInfoList W1700K::getSettingInfo() const
{
    using T = SoapySDR::ArgInfo;
    return {
        info("flatten", "Flatten", "correct the stream for the radio's decimation roll-off (for every consumer "
                                   "of the stream on this host)", T::BOOL, "false"),
        info("capture_640", "640/2 capture", "the 6 GHz band at 320 Msps, captured at 640 Msps and every other "
                                             "sample kept: flat to about +-155 MHz. Ends with the next sample "
                                             "rate or band set", T::BOOL, "false"),
    };
}

void W1700K::writeSetting(const std::string &key, const std::string &value)
{
    if (key == "flatten") {
        ring_.set_flatten(truthy(value));
    } else if (key == "capture_640") {
        std::lock_guard<std::mutex> lock(mutex_);
        need_loaded();
        if (truthy(value))
            command("configure", configure_fields(2, 320e6, 640e6), 600);
        else if (radio_.capture != radio_.rate)
            command("configure", configure_fields(radio_.band, radio_.rate, radio_.rate), 600);
    } else {
        throw std::runtime_error("w1700k: no setting " + key);
    }
}

std::string W1700K::readSetting(const std::string &key) const
{
    if (key == "flatten") {
        const auto view = ring_.view();
        return view && __atomic_load_n(&view->header()->flatten, __ATOMIC_ACQUIRE) ? "true" : "false";
    }
    if (key == "capture_640") {
        const Radio r = radio();
        return r.capture > r.rate ? "true" : "false";
    }
    throw std::runtime_error("w1700k: no setting " + key);
}

// --- registration ---

// A W1700K this host can use: its stream service is running, or the host package holds a unit's
// identity (init/install put it there). Asked for by name, it is offered whatever is found, so
// that opening it says what is missing.
SoapySDR::KwargsList find_w1700k(const SoapySDR::Kwargs &args)
{
    SoapySDR::Kwargs found = args;
    found["driver"] = "w1700k";
    std::string serial;
    int units = 0;
    const std::string data = env_or("XDG_DATA_HOME", env_or("HOME", "") + "/.local/share") + "/w1700k/units";
    if (DIR *dir = opendir(data.c_str())) {
        while (const dirent *e = readdir(dir))
            if (e->d_name[0] != '.') {
                serial = e->d_name;
                units++;
            }
        closedir(dir);
    }
    std::string name;
    try {
        name = stream_name(args);
    } catch (const std::exception &) {
        return {};  // not a stream's name: no device of ours is meant
    }
    Ring ring(name);
    if (!args.count("driver") && !units && !ring.alive()) return {};
    if (units == 1 && !found.count("serial")) found["serial"] = serial;
    if (!found.count("label")) found["label"] = "W1700K SDR" + (units == 1 ? " " + serial : std::string());
    return {found};
}

SoapySDR::Device *make_w1700k(const SoapySDR::Kwargs &args) { return new W1700K(args); }

SoapySDR::Registry register_w1700k("w1700k", &find_w1700k, &make_w1700k, SOAPY_SDR_ABI_VERSION);

}  // namespace
