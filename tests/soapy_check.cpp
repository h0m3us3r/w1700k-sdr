// SPDX-License-Identifier: MIT
// soapy_check ARGS OP...   the W1700K's SoapySDR module (host/soapy) driven through SoapySDR's
// own API, for tests/test_soapy.py and for trying a unit. ARGS are the device's ("driver=w1700k,
// ..."); each OP prints one JSON line:
//   state                     what the device reports of itself
//   antenna=NAME  freq=HZ  rate=HZ  bw=HZ  agc=0|1  gain:NAME=DB  setting:KEY=VALUE
//                             one setter; prints {"ok":true} or {"error":"..."}
//   read:FORMAT:SAMPLES:FILE  one stream: prints {"reading":true} once it is active, reads SAMPLES
//                             samples (FORMAT CS16 or CF32) into FILE (- for nowhere) or until 2 s
//                             ($SOAPY_CHECK_IDLE) pass with none, and prints what came: samples, overflows, the
//                             samples the overflows' times say were lost, reads whose time did
//                             not follow from the one before, the first and last times
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include <SoapySDR/Device.hpp>
#include <SoapySDR/Formats.hpp>

namespace {

std::string quote(const std::string &text)
{
    std::string out = "\"";
    for (char c : text) {
        if (c == '"' || c == '\\') out += '\\';
        out += c == '\n' ? ' ' : c;
    }
    return out + "\"";
}

std::string list(const std::vector<std::string> &items)
{
    std::string out = "[";
    for (size_t k = 0; k < items.size(); k++) out += (k ? "," : "") + quote(items[k]);
    return out + "]";
}

void state(SoapySDR::Device *d)
{
    const int rx = SOAPY_SDR_RX;
    std::string ranges = "[";
    for (const auto &r : d->getFrequencyRange(rx, 0)) {
        char text[96];
        std::snprintf(text, sizeof(text), "%s[%.0f,%.0f]", ranges.size() > 1 ? "," : "", r.minimum(), r.maximum());
        ranges += text;
    }
    std::string sensors = "{";
    for (const auto &key : d->listSensors())
        sensors += (sensors.size() > 1 ? "," : "") + quote(key) + ":" + quote(d->readSensor(key));
    std::string settings = "{";
    for (const auto &s : d->getSettingInfo())
        settings += (settings.size() > 1 ? "," : "") + quote(s.key) + ":" + quote(d->readSetting(s.key));
    double full = 0;
    const std::string native = d->getNativeStreamFormat(rx, 0, full);
    std::printf("{\"driver\":%s,\"hardware\":%s,\"channels\":%zu,\"antennas\":%s,\"antenna\":%s,\"freq\":%.3f,"
                "\"freq_ranges\":%s],\"rate\":%.0f,\"rates\":%zu,\"bw\":%.0f,\"agc\":%s,\"lna\":%.0f,\"lpf\":%.0f,"
                "\"gain\":%.0f,\"formats\":%s,\"native\":%s,\"full_scale\":%.0f,\"sensors\":%s},"
                "\"settings\":%s}}\n",
                quote(d->getDriverKey()).c_str(), quote(d->getHardwareKey()).c_str(), d->getNumChannels(rx),
                list(d->listAntennas(rx, 0)).c_str(), quote(d->getAntenna(rx, 0)).c_str(), d->getFrequency(rx, 0),
                ranges.c_str(), d->getSampleRate(rx, 0), d->listSampleRates(rx, 0).size(), d->getBandwidth(rx, 0),
                d->getGainMode(rx, 0) ? "true" : "false", d->getGain(rx, 0, "LNA"), d->getGain(rx, 0, "LPF"),
                d->getGain(rx, 0), list(d->getStreamFormats(rx, 0)).c_str(), quote(native).c_str(), full,
                sensors.c_str(), settings.c_str());
}

void read(SoapySDR::Device *d, const std::string &format, long long want, const std::string &path)
{
    const size_t element = format == SOAPY_SDR_CF32 ? 8 : 4;
    SoapySDR::Stream *stream = d->setupStream(SOAPY_SDR_RX, format, {0});
    const size_t mtu = d->getStreamMTU(stream);
    std::vector<char> buffer(mtu * element);
    FILE *out = path == "-" ? nullptr : std::fopen(path.c_str(), "wb");
    const int activated = d->activateStream(stream);
    const double rate = d->getSampleRate(SOAPY_SDR_RX, 0);
    std::printf("{\"reading\":%s,\"mtu\":%zu}\n", activated == 0 ? "true" : "false", mtu);
    std::fflush(stdout);

    long long samples = 0, overflows = 0, time_errors = 0, first = 0, last = 0, expected = 0, lost_ns = 0, other = 0;
    bool timed = false;
    const char *idle = std::getenv("SOAPY_CHECK_IDLE");     // seconds without a sample that end the read
    const double idle_s = idle ? std::atof(idle) : 2.0;
    const auto begun = std::chrono::steady_clock::now();
    auto last_data = begun;
    while (samples < want) {
        void *buffers[1] = {buffer.data()};
        int flags = 0;
        long long time_ns = 0;
        const size_t ask = size_t(std::min<long long>(want - samples, (long long)mtu));
        const int n = d->readStream(stream, buffers, ask, flags, time_ns, 100000);
        const auto now = std::chrono::steady_clock::now();
        if (n == SOAPY_SDR_TIMEOUT) {
            if (now - last_data > std::chrono::duration<double>(idle_s)) break;
            continue;
        }
        last_data = now;
        if (n == SOAPY_SDR_OVERFLOW) {
            overflows++;
            if (timed) lost_ns += time_ns - expected;
            expected = time_ns;
            continue;
        }
        if (n < 0) {
            other++;
            continue;
        }
        if (!(flags & SOAPY_SDR_HAS_TIME)) time_errors++;
        if (!timed) first = time_ns;
        else if (std::llabs(time_ns - expected) > 1) time_errors++;
        timed = true;
        last = time_ns;
        expected = time_ns + std::llround(double(n) * 1e9 / rate);
        if (out) std::fwrite(buffer.data(), element, size_t(n), out);
        samples += n;
    }
    const double seconds = std::chrono::duration<double>(last_data - begun).count();
    d->deactivateStream(stream);
    d->closeStream(stream);
    if (out) std::fclose(out);
    std::printf("{\"samples\":%lld,\"overflows\":%lld,\"lost_samples\":%.0f,\"time_errors\":%lld,\"errors\":%lld,"
                "\"first_ns\":%lld,\"last_ns\":%lld,\"span_ns\":%lld,\"rate\":%.0f,\"seconds\":%.3f}\n",
                samples, overflows, double(lost_ns) * rate / 1e9, time_errors, other, first, last,
                expected - first, rate, seconds);
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    SoapySDR::Device *d = nullptr;
    try {
        d = SoapySDR::Device::make(std::string(argv[1]));
    } catch (const std::exception &e) {
        std::printf("{\"error\":%s}\n", quote(e.what()).c_str());
        return 1;
    }
    const int rx = SOAPY_SDR_RX;
    int failed = 0;
    for (int k = 2; k < argc; k++) {
        const std::string op = argv[k];
        const size_t eq = op.find('=');
        const std::string key = op.substr(0, eq), value = eq == std::string::npos ? "" : op.substr(eq + 1);
        try {
            if (op == "state") {
                state(d);
            } else if (key.rfind("read:", 0) == 0) {
                const size_t a = op.find(':', 5), b = op.find(':', a + 1);
                read(d, op.substr(5, a - 5), std::atoll(op.substr(a + 1, b - a - 1).c_str()), op.substr(b + 1));
            } else {
                if (key == "antenna") d->setAntenna(rx, 0, value);
                else if (key == "freq") d->setFrequency(rx, 0, std::atof(value.c_str()));
                else if (key == "rate") d->setSampleRate(rx, 0, std::atof(value.c_str()));
                else if (key == "bw") d->setBandwidth(rx, 0, std::atof(value.c_str()));
                else if (key == "agc") d->setGainMode(rx, 0, value == "1");
                else if (key.rfind("gain:", 0) == 0) d->setGain(rx, 0, key.substr(5), std::atof(value.c_str()));
                else if (key == "gain") d->setGain(rx, 0, std::atof(value.c_str()));
                else if (key.rfind("setting:", 0) == 0) d->writeSetting(key.substr(8), value);
                else throw std::runtime_error("unknown op " + op);
                std::printf("{\"ok\":true}\n");
            }
        } catch (const std::exception &e) {
            std::printf("{\"error\":%s}\n", quote(e.what()).c_str());
            failed = 1;
        }
        std::fflush(stdout);
    }
    SoapySDR::Device::unmake(d);
    return failed;
}
