// SPDX-License-Identifier: MIT
// The correction of the radio's decimation roll-off ("flatten"). Each sample rate's decimation
// attenuates the band's edges, signal and noise alike (research ledger D293): about -2 dB at 62 %
// of Nyquist, -13 at 87 %, -22 at 99 %. The correction is a linear-phase FIR whose response is the
// inverse of that roll-off as measured on unit A (band 1, analog filter as wide as the sample
// rate), capped at +24 dB; it is zero-phase as applied: output sample t is computed from input
// samples t - kDelay .. t + kDelay, so every corrected sample keeps its index.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <fftw3.h>

class Flatten {
public:
    static constexpr unsigned kTaps = 129;
    static constexpr unsigned kDelay = (kTaps - 1) / 2;
    static constexpr unsigned kFft = 2048;                // overlap-save transform
    static constexpr unsigned kBlock = kFft - kTaps + 1;  // outputs per transform
    static constexpr double kMaxGainDb = 24;

    // The correction for a sample rate (samples/s), or nullptr when it has none.
    static const Flatten *for_rate(double sample_rate);

    double rate() const { return rate_; }
    const std::vector<double> &taps() const { return taps_; }
    // The measured roll-off at |f| = x * Nyquist, x in 0..1, dB (<= 0).
    double rolloff_db(double x) const;
    // The FIR's response at |f| = x * Nyquist, dB.
    double gain_db(double x) const;
    // The taps' kFft-point transform, divided by kFft (the inverse transform's scale folded in).
    const fftwf_complex *transfer() const { return transfer_.get(); }

private:
    Flatten(double rate, const float *rolloff);
    double rate_;
    const float *rolloff_;
    std::vector<double> taps_;
    std::unique_ptr<fftwf_complex[], void (*)(void *)> transfer_{nullptr, fftwf_free};
};

// The plans of the correction's two transforms. FFTW finds the fastest by measuring ("measured":
// 3.4 us a transform on an arm64 host where its estimate takes 5.4), in a few milliseconds where
// it has a cycle counter to time with and in most of a minute where it has not (FFTW as arm64
// distributions build it: 54 s there, during which the service did not start). So the transforms
// start on the estimated plans, at once; the measured ones are found by a process of their own,
// kept between runs, and taken up when they are there.
namespace flatten_plans {
// Where the measured plans are kept: $XDG_CACHE_HOME/w1700k/stream-fftwf-wisdom (default
// ~/.cache); "" where there is neither that nor a HOME.
std::string path();
// The transforms run on the measured plans.
bool measured();
// Looks for measured plans kept since the last look and has every FlattenRun use them; true
// when the transforms run on measured plans. For a service to call now and then.
bool refresh();
// Measures the plans and keeps them, unless they are kept already or another process is
// measuring them: what `w1700k-stream --measure-plans` does. 0, or 1 where they cannot be kept.
int measure();
// Starts that as a process of its own, at the ordinary priority, which goes on if this one ends.
// To be called before this process opens what such a process must not inherit: it is given
// none of this one's open files all the same. False if it could not be started.
bool measure_in_background();
}  // namespace flatten_plans

// One thread's overlap-save engine (its own buffer; the plans are every thread's, flatten_plans).
class FlattenRun {
public:
    FlattenRun();
    ~FlattenRun();
    FlattenRun(const FlattenRun &) = delete;
    FlattenRun &operator=(const FlattenRun &) = delete;

    // One overlap-save block, in place: the caller writes kFft complex inputs into block(), and
    // filter() leaves the kBlock outputs from block()[kTaps - 1] on; output t is centred on input
    // t + kDelay (zero-phase against the caller's indexing).
    fftwf_complex *block() { return buffer_; }
    void filter(const Flatten &f);

    // A whole array through blocks. in: n + 2 * kDelay complex samples (interleaved float I, Q),
    // the first kDelay before the first output's; out: n complex samples.
    void apply(const Flatten &f, const float *in, size_t n, float *out);

private:
    fftwf_complex *buffer_;
};
