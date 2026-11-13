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

// One thread's overlap-save engine (its own buffers and plans).
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
    fftwf_plan forward_, inverse_;
};
