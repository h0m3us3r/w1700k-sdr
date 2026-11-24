// SPDX-License-Identifier: MIT
#include "flatten.h"

#include "apart.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <stdexcept>

#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#if defined(__SSE2__)
#include <emmintrin.h>
#endif

namespace {

// The decimation's roll-off per sample rate: dB at |f| = k/64 of Nyquist, k = 0..64. Measured on
// unit A, band 1 (MT7977), from the receiver's own noise floor (which the roll-off attenuates as it
// does a signal: ledger D293, D296), analog filter as wide as the sample rate, LNA 2 / baseband 5;
// relative to 15..45 % of Nyquist, both sides averaged, 0 dB below 45 %, made monotonic and
// lightly smoothed. The 640/2 rate is flat to about 0.97 x Nyquist and has none.
constexpr unsigned kPoints = 65;
constexpr float kRolloff40[kPoints] = {
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,  -0.6f,  -1.1f,  -1.3f,  -1.5f,  -1.8f,  -2.1f,  -2.4f,  -2.7f,  -3.1f,  -3.5f,
     -3.9f,  -4.4f,  -4.9f,  -5.4f,  -5.9f,  -6.5f,  -7.2f,  -7.8f,  -8.5f,  -9.2f, -10.0f, -10.7f, -11.6f,
    -12.4f, -13.3f, -14.2f, -15.1f, -16.0f, -16.9f, -17.7f, -18.5f, -19.2f, -19.8f, -20.2f, -20.5f, -20.7f,
};
constexpr float kRolloff80[kPoints] = {
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,  -0.4f,  -0.7f,  -0.9f,  -1.1f,  -1.3f,  -1.6f,  -1.9f,  -2.2f,  -2.6f,  -3.0f,
     -3.4f,  -3.8f,  -4.3f,  -4.8f,  -5.3f,  -5.9f,  -6.5f,  -7.1f,  -7.8f,  -8.5f,  -9.3f, -10.1f, -11.0f,
    -11.8f, -12.8f, -13.8f, -14.8f, -15.9f, -17.0f, -18.2f, -19.3f, -20.5f, -21.5f, -22.4f, -23.1f, -23.4f,
};
constexpr float kRolloff160[kPoints] = {
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,  -0.3f,  -0.6f,  -0.8f,  -1.0f,  -1.2f,  -1.5f,  -1.7f,  -2.1f,  -2.4f,  -2.8f,
     -3.1f,  -3.6f,  -4.1f,  -4.6f,  -5.1f,  -5.7f,  -6.3f,  -6.9f,  -7.6f,  -8.4f,  -9.1f,  -9.9f, -10.8f,
    -11.7f, -12.7f, -13.7f, -14.8f, -15.9f, -17.1f, -18.4f, -19.7f, -21.0f, -22.3f, -23.4f, -24.2f, -24.7f,
};
constexpr float kRolloff320[kPoints] = {
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,   0.0f,
      0.0f,   0.0f,   0.0f,  -0.1f,  -0.2f,  -0.3f,  -0.5f,  -0.6f,  -0.8f,  -1.1f,  -1.4f,  -1.7f,  -2.0f,
     -2.4f,  -2.8f,  -3.2f,  -3.6f,  -4.1f,  -4.6f,  -5.1f,  -5.7f,  -6.3f,  -6.9f,  -7.6f,  -8.4f,  -9.2f,
    -10.1f, -11.0f, -12.0f, -13.0f, -14.0f, -15.2f, -16.4f, -17.8f, -19.1f, -20.5f, -21.7f, -22.6f, -23.1f,
};

constexpr unsigned kDesignPoints = 8192;  // frequency-sampling grid of the FIR design
constexpr double kKaiserBeta = 4;         // window of the 129 taps (<= 0.15 dB error to 0.95 x Nyquist)

std::mutex planner;  // FFTW's planner is not thread-safe

double interpolate(const float *table, double x)
{
    x = std::min(std::max(x, 0.0), 1.0) * (kPoints - 1);
    const unsigned k = std::min(unsigned(x), kPoints - 2);
    return table[k] + (x - k) * (table[k + 1] - table[k]);
}

double bessel_i0(double x)
{
    double sum = 1, term = 1;
    for (int k = 1; k < 50; ++k) {
        term *= (x / (2 * k)) * (x / (2 * k));
        sum += term;
    }
    return sum;
}

}  // namespace

const Flatten *Flatten::for_rate(double sample_rate)
{
    static std::mutex m;
    static std::map<long long, std::unique_ptr<Flatten>> made;
    const float *table = nullptr;
    const long long rate = std::llround(sample_rate);
    if (rate == 40000000) table = kRolloff40;
    else if (rate == 80000000) table = kRolloff80;
    else if (rate == 160000000) table = kRolloff160;
    else if (rate == 320000000) table = kRolloff320;
    if (!table) return nullptr;
    std::lock_guard<std::mutex> lock(m);
    auto &f = made[rate];
    if (!f) f.reset(new Flatten(double(rate), table));
    return f.get();
}

// Frequency sampling: the inverse of the roll-off (capped) on a fine grid, back to the time
// domain, the kTaps around zero kept under a Kaiser window. The gain is real and even, so the taps
// are real and symmetric: linear phase, delay kDelay.
Flatten::Flatten(double rate, const float *rolloff) : rate_(rate), rolloff_(rolloff), taps_(kTaps)
{
    std::vector<double> gain(kDesignPoints), impulse(kDesignPoints, 0.0);
    const double cap = std::pow(10.0, kMaxGainDb / 20);
    for (unsigned k = 0; k < kDesignPoints; ++k) {
        const double f = k < kDesignPoints / 2 ? double(k) / kDesignPoints : double(k) / kDesignPoints - 1;
        gain[k] = std::min(std::pow(10.0, -interpolate(rolloff, std::fabs(f) / 0.5) / 20), cap);
    }
    // The inverse DFT of a real even sequence: h[n] = (1/K) sum_k G[k] cos(2 pi k n / K).
    for (unsigned n = 0; n <= kDelay; ++n) {
        double sum = 0;
        for (unsigned k = 0; k < kDesignPoints; ++k) sum += gain[k] * std::cos(2 * M_PI * double(k) * n / kDesignPoints);
        impulse[n] = sum / kDesignPoints;
    }
    const double i0 = bessel_i0(kKaiserBeta);
    for (unsigned t = 0; t < kTaps; ++t) {
        const int n = int(t) - int(kDelay);
        const double r = double(n) / kDelay;
        taps_[t] = impulse[unsigned(std::abs(n))] * bessel_i0(kKaiserBeta * std::sqrt(std::max(0.0, 1 - r * r))) / i0;
    }
    // The transfer of the causal taps h[0..kTaps) for overlap-save, scaled by 1/kFft.
    transfer_.reset(fftwf_alloc_complex(kFft));
    fftwf_complex *in = fftwf_alloc_complex(kFft);
    for (unsigned k = 0; k < kFft; ++k) {
        in[k][0] = k < kTaps ? float(taps_[k] / kFft) : 0.0f;
        in[k][1] = 0;
    }
    {
        std::lock_guard<std::mutex> lock(planner);
        fftwf_plan p = fftwf_plan_dft_1d(int(kFft), in, transfer_.get(), FFTW_FORWARD, FFTW_ESTIMATE);
        fftwf_execute(p);
        fftwf_destroy_plan(p);
    }
    fftwf_free(in);
}

double Flatten::rolloff_db(double x) const { return interpolate(rolloff_, x); }

double Flatten::gain_db(double x) const
{
    const double w = M_PI * std::min(std::max(x, 0.0), 1.0);  // radians/sample
    double re = 0;
    for (unsigned t = 0; t < kTaps; ++t) re += taps_[t] * std::cos(w * (double(t) - kDelay));
    return 20 * std::log10(std::fabs(re));
}

// ---- the transforms' plans (flatten.h, flatten_plans) ------------------------------------------------

namespace {

// One pair for every thread: a plan is made on a buffer of its own and run on each thread's
// (FFTW's execute for another array of the same alignment, which any number of threads may be
// in at once). A pair that was put to use is never destroyed, since a thread may be in it; the
// one estimated pair a process leaves behind when it moves to the measured one is two small plans.
struct Plans {
    fftwf_plan forward = nullptr, inverse = nullptr;
    bool measured = false;
};
std::atomic<const Plans *> plans_in_use{nullptr};
struct timespec kept_read = {0, 0};  // the kept plans' modification time when last read; with the planner held

std::string kept_path(bool create)
{
    std::string dir;
    if (const char *cache = getenv("XDG_CACHE_HOME"); cache && *cache) dir = cache;
    else if (const char *home = getenv("HOME"); home && *home) dir = std::string(home) + "/.cache";
    else return "";
    if (create) mkdir(dir.c_str(), 0700);
    dir += "/w1700k";
    if (create) mkdir(dir.c_str(), 0700);
    return dir + "/stream-fftwf-wisdom";
}

// With the planner held. Gives FFTW the kept plans if they were written since they were last
// read; whether it did.
bool read_kept()
{
    const std::string path = kept_path(false);
    struct stat st;
    if (path.empty() || stat(path.c_str(), &st)) return false;
    if (st.st_mtim.tv_sec == kept_read.tv_sec && st.st_mtim.tv_nsec == kept_read.tv_nsec) return false;
    kept_read = st.st_mtim;
    return fftwf_import_wisdom_from_filename(path.c_str()) != 0;
}

// With the planner held. The measured pair where FFTW knows both plans, else (measure: found
// now, however long that takes; otherwise) the estimated pair.
Plans *plan(bool measure)
{
    const int n = int(Flatten::kFft);
    fftwf_complex *on = fftwf_alloc_complex(Flatten::kFft);
    auto pair = std::make_unique<Plans>();
    const unsigned how = measure ? FFTW_MEASURE : FFTW_MEASURE | FFTW_WISDOM_ONLY;
    pair->forward = fftwf_plan_dft_1d(n, on, on, FFTW_FORWARD, how);
    pair->inverse = fftwf_plan_dft_1d(n, on, on, FFTW_BACKWARD, how);
    pair->measured = pair->forward && pair->inverse;
    if (!pair->measured) {
        if (pair->forward) fftwf_destroy_plan(pair->forward);
        if (pair->inverse) fftwf_destroy_plan(pair->inverse);
        pair->forward = fftwf_plan_dft_1d(n, on, on, FFTW_FORWARD, FFTW_ESTIMATE);
        pair->inverse = fftwf_plan_dft_1d(n, on, on, FFTW_BACKWARD, FFTW_ESTIMATE);
    }
    fftwf_free(on);
    if (!pair->forward || !pair->inverse) throw std::runtime_error("FFTW could not plan the correction's transforms");
    return pair.release();
}

void discard(Plans *pair)  // with the planner held; a pair never put to use
{
    fftwf_destroy_plan(pair->forward);
    fftwf_destroy_plan(pair->inverse);
    delete pair;
}

// The pair in use: from the first call on, the kept measured plans or the estimate.
const Plans *plans()
{
    if (const Plans *pair = plans_in_use.load(std::memory_order_acquire)) return pair;
    std::lock_guard<std::mutex> lock(planner);
    if (const Plans *pair = plans_in_use.load(std::memory_order_acquire)) return pair;
    read_kept();
    const Plans *pair = plan(false);
    plans_in_use.store(pair, std::memory_order_release);
    return pair;
}

}  // namespace

std::string flatten_plans::path() { return kept_path(false); }

bool flatten_plans::measured() { return plans()->measured; }

bool flatten_plans::refresh()
{
    if (plans()->measured) return true;
    std::lock_guard<std::mutex> lock(planner);
    if (!read_kept()) return false;
    Plans *pair = plan(false);
    if (!pair->measured) {
        discard(pair);
        return false;
    }
    plans_in_use.store(pair, std::memory_order_release);  // the estimated pair stays: a thread may be in it
    return true;
}

int flatten_plans::measure()
{
    const std::string path = kept_path(true);
    if (path.empty()) return 1;
    // One process measures at a time, and each asks first whether it is still needed: several
    // services started together (a test run) start as many of these.
    const int held = open((path + ".lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (held < 0) return 1;
    if (flock(held, LOCK_EX | LOCK_NB)) return 0;
    std::lock_guard<std::mutex> lock(planner);
    read_kept();
    Plans *pair = plan(false);
    const bool kept = pair->measured;
    discard(pair);
    if (kept) return 0;
    discard(plan(true));
    read_kept();  // what another program kept meanwhile stays kept: FFTW adds a file to what it knows
    const std::string fresh = path + ".new";
    if (!fftwf_export_wisdom_to_filename(fresh.c_str()) || std::rename(fresh.c_str(), path.c_str())) return 1;
    return 0;
}

bool flatten_plans::measure_in_background()
{
    // At the ordinary priority, whatever this process runs at: it measures by the clock.
    return apart::run({"--measure-plans"});
}

FlattenRun::FlattenRun()
{
    buffer_ = fftwf_alloc_complex(Flatten::kFft);
    if (!buffer_) throw std::bad_alloc();
    plans();  // planned now, not when the first block is filtered
}

FlattenRun::~FlattenRun() { fftwf_free(buffer_); }

namespace {

// x[k] *= h[k] for `count` (even) complex values, interleaved real and imaginary parts.
void multiply(fftwf_complex *x, const fftwf_complex *h, unsigned count)
{
    unsigned k = 0;
#if defined(__SSE2__)
    // Two at a time: (a + jb)(c + jd) = (ac - bd) + j(ad + bc), as [a, a] * [c, d] + [-b, b] * [d, c].
    const __m128 negate_even = _mm_castsi128_ps(_mm_set_epi32(0, int(0x80000000u), 0, int(0x80000000u)));
    for (; k + 2 <= count; k += 2) {
        const __m128 v = _mm_load_ps(x[k]), t = _mm_load_ps(h[k]);
        const __m128 re = _mm_shuffle_ps(v, v, _MM_SHUFFLE(2, 2, 0, 0)), im = _mm_shuffle_ps(v, v, _MM_SHUFFLE(3, 3, 1, 1));
        const __m128 swapped = _mm_shuffle_ps(t, t, _MM_SHUFFLE(2, 3, 0, 1));
        _mm_store_ps(x[k], _mm_add_ps(_mm_mul_ps(re, t), _mm_xor_ps(_mm_mul_ps(im, swapped), negate_even)));
    }
#endif
    for (; k < count; ++k) {
        const float re = x[k][0] * h[k][0] - x[k][1] * h[k][1];
        const float im = x[k][0] * h[k][1] + x[k][1] * h[k][0];
        x[k][0] = re;
        x[k][1] = im;
    }
}

}  // namespace

// Overlap-save: transform kFft inputs, multiply by the taps' transfer, transform back; the last
// kBlock outputs are free of wrap-around: output t is the full convolution at t + kTaps - 1, i.e.
// centred on input t + kDelay.
void FlattenRun::filter(const Flatten &f)
{
    const Plans *pair = plans();
    fftwf_execute_dft(pair->forward, buffer_, buffer_);
    multiply(buffer_, f.transfer(), Flatten::kFft);
    fftwf_execute_dft(pair->inverse, buffer_, buffer_);
}

// A complex sample is two floats in both the caller's arrays and FFTW's, so blocks move in and out
// as they are.
void FlattenRun::apply(const Flatten &f, const float *in, size_t n, float *out)
{
    const size_t total = n + Flatten::kTaps - 1;
    for (size_t j = 0; j < n; j += Flatten::kBlock) {
        const size_t have = std::min<size_t>(Flatten::kFft, total - j);  // zeros beyond the input
        std::memcpy(buffer_, in + 2 * j, have * sizeof(fftwf_complex));
        if (have < Flatten::kFft) std::memset(buffer_ + have, 0, (Flatten::kFft - have) * sizeof(fftwf_complex));
        filter(f);
        const size_t m = std::min<size_t>(Flatten::kBlock, n - j);
        std::memcpy(out + 2 * j, buffer_ + Flatten::kTaps - 1, m * sizeof(fftwf_complex));
    }
}
