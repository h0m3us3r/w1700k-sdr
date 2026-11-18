// SPDX-License-Identifier: MIT
// w1700k-spectrum's options.
#pragma once

#include <string>
#include <vector>

#include "w1700k_stream.h"

struct ServeOptions {
    std::string listen = "127.0.0.1:8073";    // HTTP address:port
    std::string web_dir;                      // serve the UI from here instead of the built-in copy
    // Who is answered (web.h): names beside the server's own, other origins for the socket,
    // whether it sits behind a proxy, and whether it wants its token beyond this machine.
    std::vector<std::string> hosts, origins;
    bool behind_proxy = false, token = true;
    std::string token_value;                  // the token to want, from the environment; empty: one is made
    std::string stream = W1700K_STREAM_NAME;  // w1700k-stream's shared memory
    unsigned fft_threads = 4;                 // FFT workers
    std::string controller = "w1700k serve";  // the receiver's controller; empty: a passive viewer
    // The stream's tuning until the controller reports it, and always for a passive viewer.
    double center_hz = 5250e6;
    double sample_rate = 320e6;
    // SIGUSR1 writes the newest dump_seconds of samples (complex int16) and a .json beside them.
    double dump_seconds = 0.3;
    std::string dump_dir = "/tmp";
};

int command_serve(const ServeOptions &options);
