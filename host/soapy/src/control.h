// SPDX-License-Identifier: MIT
// The receiver's controller for one caller at a time: a child process (by default `w1700k serve`)
// speaking newline-delimited JSON, as host/w1700k/README.md describes it. A request is written and
// its reply waited for; the state and log events that arrive meanwhile are taken in passing.
#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include <sys/types.h>

#include "json.h"

class Control {
public:
    using Log = std::function<void(const std::string &level, const std::string &text)>;

    // Starts `sh -c command` and waits for the controller's first state (it has then read what the
    // unit holds). Throws std::runtime_error with what the controller said was wrong.
    // `command` is run by sh -c; `arguments` are appended to what it runs as words of their own
    // (the shell's "$@"), whatever they hold.
    Control(std::string command, std::vector<std::string> arguments, Log log, double start_timeout_s = 30);
    ~Control();
    Control(const Control &) = delete;
    Control &operator=(const Control &) = delete;

    // {"id":N,"op":op<fields>}, with fields as JSON text such as ",\"hz\":5250000000"; returns once
    // the controller has answered. Throws std::runtime_error with its error, or when it does not
    // answer within the timeout or has ended.
    void request(const std::string &op, const std::string &fields, double timeout_s);
    // Takes in what has arrived without waiting, then the latest state.
    const JsonValue &state();
    // Counts the states received: unchanged, and state() is what it was.
    uint64_t state_serial() const { return state_serial_; }
    bool running() const { return fd_ >= 0; }

private:
    void spawn(double timeout_s);
    void stop();
    // One line into `line`, waiting until `deadline` (steady seconds). False at the deadline;
    // throws when the controller ended.
    bool read_line(std::string &line, double deadline);
    // An event: taken in. A reply: true, with `m` left to the caller.
    bool is_reply(const JsonValue &m);

    const std::string command_;
    const std::vector<std::string> arguments_;
    Log log_;
    pid_t pid_ = -1;
    int fd_ = -1;               // one socket: the child's stdin and stdout
    std::string buffer_;        // received, not yet a whole line
    std::string trouble_;       // the last error the controller logged
    uint64_t next_id_ = 1;
    JsonValue state_;
    bool have_state_ = false;
    uint64_t state_serial_ = 0;
};
