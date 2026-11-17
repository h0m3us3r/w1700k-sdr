// SPDX-License-Identifier: MIT
#include "control.h"

#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <stdexcept>
#include <thread>

#include <poll.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

namespace {

double now_s()
{
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

}  // namespace

Control::Control(std::string command, std::vector<std::string> arguments, Log log, double start_timeout_s)
    : command_(std::move(command)), arguments_(std::move(arguments)), log_(std::move(log))
{
    spawn(start_timeout_s);
}

Control::~Control() { stop(); }

void Control::spawn(double timeout_s)
{
    // A socket rather than pipes: this code runs inside whatever application loaded the module,
    // and a write to a controller that has gone must fail here, not signal the application.
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair))
        throw std::runtime_error(std::string("socketpair: ") + std::strerror(errno));
    // sh -c 'COMMAND "$@"' sh ARGUMENT...: the arguments reach the command as they are.
    const std::string text = arguments_.empty() ? command_ : command_ + " \"$@\"";
    std::vector<char *> argv = {const_cast<char *>("sh"), const_cast<char *>("-c"), const_cast<char *>(text.c_str()),
                                const_cast<char *>("sh")};
    for (const auto &word : arguments_) argv.push_back(const_cast<char *>(word.c_str()));
    argv.push_back(nullptr);
    pid_ = fork();
    if (pid_ == 0) {
        // Async-signal-safe calls only until exec: the application has other threads.
        setpgid(0, 0);  // a Ctrl-C at the application's terminal is the application's to handle
        signal(SIGPIPE, SIG_DFL);
        if (dup2(pair[1], STDIN_FILENO) < 0 || dup2(pair[1], STDOUT_FILENO) < 0) _exit(127);
        execv("/bin/sh", argv.data());
        _exit(127);
    }
    const int e = errno;
    close(pair[1]);
    if (pid_ < 0) {
        close(pair[0]);
        throw std::runtime_error(std::string("fork: ") + std::strerror(e));
    }
    setpgid(pid_, pid_);
    fd_ = pair[0];
    buffer_.clear();
    trouble_.clear();
    have_state_ = false;

    // Its first state comes once it has read what the unit holds; an error logged before that
    // (the unit cannot be reached) is why there will be none.
    const double deadline = now_s() + timeout_s;
    std::string line, why;
    try {
        while (!have_state_ && trouble_.empty()) {
            if (!read_line(line, deadline)) {
                why = "gave no state within " + std::to_string(int(timeout_s)) + " s";
                break;
            }
            JsonValue m;
            try {
                m = parse_json(line);
            } catch (const std::exception &) {
                continue;
            }
            is_reply(m);
        }
    } catch (const std::exception &ex) {
        why = ex.what();
    }
    if (!have_state_) {
        if (!trouble_.empty()) why = trouble_;
        stop();
        throw std::runtime_error("the controller `" + command_ + "`: " + why);
    }
}

void Control::stop()
{
    if (pid_ < 0) return;
    if (fd_ >= 0) shutdown(fd_, SHUT_WR);  // end of input: the controller closes down and exits
    int status;
    bool reaped = false;
    auto wait_for = [&](double seconds) {
        const double until = now_s() + seconds;
        while (!reaped && now_s() < until) {
            const pid_t r = waitpid(pid_, &status, WNOHANG);
            // ECHILD: the application reaps every child itself, and has
            reaped = r == pid_ || (r < 0 && errno == ECHILD);
            if (!reaped) std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        return reaped;
    };
    if (!wait_for(3)) {
        kill(-pid_, SIGTERM);
        if (!wait_for(1)) {
            kill(-pid_, SIGKILL);
            while (waitpid(pid_, &status, 0) < 0 && errno == EINTR) {
            }
        }
    }
    kill(-pid_, SIGTERM);  // anything the shell started that outlived it
    if (fd_ >= 0) close(fd_);
    fd_ = -1;
    pid_ = -1;
}

bool Control::read_line(std::string &line, double deadline)
{
    for (;;) {
        const size_t nl = buffer_.find('\n');
        if (nl != std::string::npos) {
            line = buffer_.substr(0, nl);
            buffer_.erase(0, nl + 1);
            return true;
        }
        if (fd_ < 0) throw std::runtime_error("the controller is not running");
        const double left = deadline - now_s();
        pollfd p{fd_, POLLIN, 0};
        const int r = poll(&p, 1, left > 0 ? int(left * 1000) + 1 : 0);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return false;
        char chunk[65536];
        const ssize_t n = recv(fd_, chunk, sizeof(chunk), 0);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n <= 0) {
            close(fd_);
            fd_ = -1;
            throw std::runtime_error("the controller ended" + (trouble_.empty() ? "" : ": " + trouble_));
        }
        buffer_.append(chunk, size_t(n));
        if (buffer_.size() > (1u << 22)) buffer_.clear();  // not a controller's output
    }
}

bool Control::is_reply(const JsonValue &m)
{
    if (const JsonValue *state = m.find("state")) {  // an event's, or a reply's
        state_ = *state;
        have_state_ = true;
        state_serial_++;
    }
    if (m.has("id")) return true;
    const std::string event = m.string_or("event");
    if (event == "log") {
        const std::string level = m.string_or("level", "info"), text = m.string_or("text");
        if (level == "error") trouble_ = text;
        if (log_) log_(level, text);
    } else if (event == "progress") {
        if (log_) log_("debug", m.string_or("text"));
    }
    return false;
}

void Control::request(const std::string &op, const std::string &fields, double timeout_s)
{
    if (fd_ < 0) {  // it ended: once more, from the unit's state
        stop();
        spawn(30);
    }
    const uint64_t id = next_id_++;
    const std::string text = "{\"id\":" + std::to_string(id) + ",\"op\":" + json_quote(op) + fields + "}\n";
    for (size_t sent = 0; sent < text.size();) {
        const ssize_t n = send(fd_, text.data() + sent, text.size() - sent, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) {
            close(fd_);
            fd_ = -1;
            throw std::runtime_error("the controller ended");
        }
        sent += size_t(n);
    }
    const double deadline = now_s() + timeout_s;
    std::string line;
    for (;;) {
        if (!read_line(line, deadline)) {
            // What it is doing is unknown from here on: a new one reads the unit's state afresh.
            stop();
            throw std::runtime_error("the controller did not answer " + op + " within " +
                                     std::to_string(int(timeout_s)) + " s");
        }
        JsonValue m;
        try {
            m = parse_json(line);
        } catch (const std::exception &) {
            continue;
        }
        if (!is_reply(m) || uint64_t(m.number_or("id", 0)) != id) continue;
        if (!m.boolean_or("ok", false)) throw std::runtime_error(m.string_or("error", op + " failed"));
        return;
    }
}

const JsonValue &Control::state()
{
    std::string line;
    try {
        while (fd_ >= 0 && read_line(line, 0)) {
            try {
                is_reply(parse_json(line));
            } catch (const std::exception &) {
            }
        }
    } catch (const std::exception &) {  // it ended: the last state stands until the next request
    }
    return state_;
}
