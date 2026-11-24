// SPDX-License-Identifier: MIT
// w1700k-spectrum: a live spectrum and waterfall of the W1700K's stream, as w1700k-stream serves
// it, in a browser, with the receiver's controls.
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>

#include "serve.h"
#include "spectrum.h"

namespace {

void usage()
{
    std::fprintf(stderr,
                 "usage: w1700k-spectrum [options]\n"
                 "  --listen ADDR:PORT  the web page (default 127.0.0.1:8073)\n"
                 "  --host NAME         a name the server answers to, beside its own addresses, this machine's\n"
                 "                      name and localhost (a DNS name, a proxy's); may be given more than once\n"
                 "  --allow-origin URL  another origin whose pages may open the socket (a front end served\n"
                 "                      from elsewhere), as scheme://host[:port]; may be given more than once\n"
                 "  --behind-proxy      a reverse proxy passes requests on: the name is its X-Forwarded-Host\n"
                 "  --no-token          listening beyond this machine, want no access token (default: the\n"
                 "                      server makes one and prints the link that carries it; a token of\n"
                 "                      your own, 16 or more of A-Z a-z 0-9 _ -, in W1700K_SPECTRUM_TOKEN)\n"
                 "  --web DIR           serve the page from DIR instead of the built-in copy\n"
                 "  --stream NAME       w1700k-stream's shared memory (default /w1700k-stream)\n"
                 "  --fft-threads N     FFT workers (default 4)\n"
                 "  --controller CMD    the receiver's controller, run with sh -c (default \"w1700k serve\")\n"
                 "  --no-controller     a passive viewer: no receiver controls\n"
                 "  --center MHZ        the stream's centre frequency until the controller reports it (default 5250)\n"
                 "  --rate MSPS         its sample rate until the controller reports it (default 320)\n"
                 "  --dump-dir DIR      where SIGUSR1 writes a snapshot of the newest samples (default /tmp)\n"
                 "  --dump-seconds S    its length, at most the ring's (default 0.3)\n"
                 "  --measure-plan N    serve nothing: find FFTW's measured plan of FFT size N and keep it for\n"
                 "                      later runs, as a server that lacks it has done in the background by itself\n");
}

unsigned positive(const std::string &option, const std::string &text)
{
    size_t used = 0;
    unsigned long v = 0;
    try {
        v = std::stoul(text, &used);
    } catch (const std::logic_error &) {
        used = 0;
    }
    if (!used || used != text.size() || v == 0 || v > 1u << 20)
        throw std::runtime_error(option + " needs a positive integer, not \"" + text + "\"");
    return unsigned(v);
}

double number(const std::string &option, const std::string &text)
{
    size_t used = 0;
    double v = 0;
    try {
        v = std::stod(text, &used);
    } catch (const std::logic_error &) {
        used = 0;
    }
    if (!used || used != text.size() || !(v > 0))
        throw std::runtime_error(option + " needs a positive number, not \"" + text + "\"");
    return v;
}

}  // namespace

int main(int argc, char **argv)
{
    ServeOptions o;
    try {
        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i];
            auto value = [&]() -> std::string {
                if (i + 1 >= argc) throw std::runtime_error(arg + " needs a value");
                return argv[++i];
            };
            if (arg == "--listen") o.listen = value();
            else if (arg == "--web") o.web_dir = value();
            else if (arg == "--host") o.hosts.push_back(value());
            else if (arg == "--allow-origin") o.origins.push_back(value());
            else if (arg == "--behind-proxy") o.behind_proxy = true;
            else if (arg == "--no-token") o.token = false;
            else if (arg == "--stream") o.stream = value();
            else if (arg == "--fft-threads") o.fft_threads = positive(arg, value());
            else if (arg == "--controller") o.controller = value();
            else if (arg == "--no-controller") o.controller.clear();
            else if (arg == "--center") o.center_hz = number(arg, value()) * 1e6;
            else if (arg == "--rate") o.sample_rate = number(arg, value()) * 1e6;
            else if (arg == "--dump-dir") o.dump_dir = value();
            else if (arg == "--dump-seconds") o.dump_seconds = number(arg, value());
            else if (arg == "--measure-plan") return spectrum_measure_plan(positive(arg, value()));
            else if (arg == "-h" || arg == "--help") {
                usage();
                return 0;
            } else {
                throw std::runtime_error("unknown option " + arg);
            }
        }
        if (o.controller.find_first_not_of(" \t") == std::string::npos) o.controller.clear();
        if (const char *token = std::getenv("W1700K_SPECTRUM_TOKEN"); token && *token) {
            o.token_value = token;
            if (o.token_value.size() < 16 ||
                o.token_value.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") != std::string::npos)
                throw std::runtime_error("W1700K_SPECTRUM_TOKEN has to be 16 or more of A-Z a-z 0-9 _ -");
        }
        for (const auto &origin : o.origins)
            if (origin.find("://") == std::string::npos)
                throw std::runtime_error("--allow-origin needs scheme://host[:port], not \"" + origin + "\"");
    } catch (const std::exception &e) {
        std::fprintf(stderr, "w1700k-spectrum: %s\n", e.what());
        usage();
        return 2;
    }
    try {
        return command_serve(o);
    } catch (const std::exception &e) {
        std::fprintf(stderr, "w1700k-spectrum: %s\n", e.what());
        return 1;
    }
}
