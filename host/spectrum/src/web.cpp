// SPDX-License-Identifier: MIT
#include "web.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include <arpa/inet.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <unistd.h>
#include <zlib.h>

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

namespace {

constexpr size_t kMaxRequestHeader = 16384;
constexpr size_t kMaxMessage = 65536;           // client messages are small
// A reader that asks for more while this much of what it asked for is still unsent is not
// reading: it gets what is queued and the connection is closed. (Each answer was appended, and a
// megabyte of requests for the page's script was a gigabyte of answers held here.)
constexpr size_t kHttpBacklog = 1 << 20;
constexpr size_t kMaxConnections = 64;
constexpr auto kCloseWithin = 10s;              // what a closing connection has to take its last bytes
constexpr auto kAcceptPause = 250ms;            // no descriptor for a new connection: look again then
constexpr size_t kMinCompressed = 64;           // shorter messages go uncompressed
constexpr auto kPingInterval = 10s;
constexpr auto kIdleTimeout = 40s;              // nothing received: the peer is gone
const char *kWebSocketGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

// SHA-1 (FIPS 180-4), used only for the WebSocket handshake.
std::array<uint8_t, 20> sha1(const std::string &message)
{
    uint32_t h[5] = {0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0};
    std::string data = message;
    uint64_t bits = uint64_t(message.size()) * 8;
    data += char(0x80);
    while (data.size() % 64 != 56) data += char(0);
    for (int i = 7; i >= 0; --i) data += char(bits >> (8 * i));
    auto rotate = [](uint32_t x, int n) { return (x << n) | (x >> (32 - n)); };
    for (size_t block = 0; block < data.size(); block += 64) {
        uint32_t w[80];
        for (int i = 0; i < 16; ++i)
            w[i] = uint32_t(uint8_t(data[block + 4 * i])) << 24 | uint32_t(uint8_t(data[block + 4 * i + 1])) << 16 |
                   uint32_t(uint8_t(data[block + 4 * i + 2])) << 8 | uint32_t(uint8_t(data[block + 4 * i + 3]));
        for (int i = 16; i < 80; ++i) w[i] = rotate(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
        for (int i = 0; i < 80; ++i) {
            uint32_t f, k;
            if (i < 20) f = (b & c) | (~b & d), k = 0x5A827999;
            else if (i < 40) f = b ^ c ^ d, k = 0x6ED9EBA1;
            else if (i < 60) f = (b & c) | (b & d) | (c & d), k = 0x8F1BBCDC;
            else f = b ^ c ^ d, k = 0xCA62C1D6;
            uint32_t t = rotate(a, 5) + f + e + k + w[i];
            e = d, d = c, c = rotate(b, 30), b = a, a = t;
        }
        h[0] += a, h[1] += b, h[2] += c, h[3] += d, h[4] += e;
    }
    std::array<uint8_t, 20> digest;
    for (int i = 0; i < 20; ++i) digest[i] = uint8_t(h[i / 4] >> (24 - 8 * (i % 4)));
    return digest;
}

std::string base64(const uint8_t *data, size_t size)
{
    static const char *digits = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string text;
    for (size_t i = 0; i < size; i += 3) {
        uint32_t n = uint32_t(data[i]) << 16 | (i + 1 < size ? uint32_t(data[i + 1]) << 8 : 0) |
                     (i + 2 < size ? data[i + 2] : 0);
        text += digits[n >> 18 & 63];
        text += digits[n >> 12 & 63];
        text += i + 1 < size ? digits[n >> 6 & 63] : '=';
        text += i + 2 < size ? digits[n & 63] : '=';
    }
    return text;
}

std::string lower(std::string text)
{
    for (char &c : text) c = char(std::tolower(uint8_t(c)));
    return text;
}

std::string trim(const std::string &text)
{
    size_t first = text.find_first_not_of(" \t"), last = text.find_last_not_of(" \t");
    return first == std::string::npos ? "" : text.substr(first, last - first + 1);
}

// The comma- or semicolon-separated items of a header value, trimmed.
std::vector<std::string> split(const std::string &text, char separator)
{
    std::vector<std::string> items;
    std::stringstream stream(text);
    std::string item;
    while (std::getline(stream, item, separator)) items.push_back(trim(item));
    return items;
}

bool has_token(const std::string &value, const std::string &token)
{
    for (const auto &item : split(lower(value), ','))
        if (item == token) return true;
    return false;
}

std::string content_type(const std::string &path)
{
    auto ends = [&](const char *suffix) {
        size_t n = std::strlen(suffix);
        return path.size() >= n && path.compare(path.size() - n, n, suffix) == 0;
    };
    if (ends(".html")) return "text/html; charset=utf-8";
    if (ends(".js")) return "text/javascript; charset=utf-8";
    if (ends(".css")) return "text/css; charset=utf-8";
    if (ends(".svg")) return "image/svg+xml";
    if (ends(".json")) return "application/json";
    if (ends(".png")) return "image/png";
    return "application/octet-stream";
}

std::string gzip(const std::string &data)
{
    z_stream z{};
    if (deflateInit2(&z, 9, Z_DEFLATED, 31, 9, Z_DEFAULT_STRATEGY) != Z_OK) return "";
    std::string out(deflateBound(&z, uLong(data.size())) + 32, '\0');
    z.next_in = (Bytef *)data.data();
    z.avail_in = uInt(data.size());
    z.next_out = (Bytef *)&out[0];
    z.avail_out = uInt(out.size());
    int status = deflate(&z, Z_FINISH);
    out.resize(z.total_out);
    deflateEnd(&z);
    return status == Z_STREAM_END ? out : "";
}

std::string etag(const std::string &data)
{
    uint64_t hash = 1469598103934665603ull;  // FNV-1a
    for (unsigned char c : data) hash = (hash ^ c) * 1099511628211ull;
    char text[24];
    std::snprintf(text, sizeof(text), "\"%016llx\"", (unsigned long long)hash);
    return text;
}

void set_nonblocking(int fd) { fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK); }

// Equal, in a time that does not say where they differ.
bool same_secret(const std::string &a, const std::string &b)
{
    unsigned char differ = a.size() != b.size();
    for (size_t i = 0; i < a.size() && i < b.size(); ++i) differ |= static_cast<unsigned char>(a[i] ^ b[i]);
    return !differ;
}

std::string random_token()
{
    unsigned char bytes[16];
    if (getrandom(bytes, sizeof(bytes), 0) != ssize_t(sizeof(bytes))) throw std::runtime_error("no random bytes for the access token");
    static const char digits[] = "0123456789abcdef";
    std::string text;
    for (unsigned char b : bytes) {
        text += digits[b >> 4];
        text += digits[b & 15];
    }
    return text;
}

// "name=value" among `fields`, which `separator` divides: a query's or a Cookie header's.
std::string field(const std::string &fields, char separator, const std::string &name)
{
    for (const auto &part : split(fields, separator)) {
        const std::string one = trim(part);
        if (one.compare(0, name.size() + 1, name + "=") == 0) return one.substr(name.size() + 1);
    }
    return {};
}

// "NAME" or "NAME:PORT" or "[v6]:PORT", in lower case: the name (a v6 address in its brackets)
// and the port. False for what is neither.
bool split_host(const std::string &host, std::string &name, std::string &port)
{
    port.clear();
    name = lower(trim(host));
    if (!name.empty() && name[0] == '[') {
        const size_t end = name.find(']');
        if (end == std::string::npos || (end + 1 < name.size() && name[end + 1] != ':')) return false;
        if (end + 1 < name.size()) port = name.substr(end + 2);
        name.resize(end + 1);
    } else if (const size_t colon = name.rfind(':'); colon != std::string::npos) {
        port = name.substr(colon + 1);
        name.resize(colon);
    }
    return !name.empty() && name.find_first_of("/\\@ \t") == std::string::npos;
}

}  // namespace

struct WebServer::Asset {
    std::string path, type, body, gzipped, tag;
};

struct WebServer::Connection {
    unsigned id = 0;
    int fd = -1;
    std::string peer;
    std::string input, output;
    size_t output_at = 0;          // bytes of `output` already written
    bool websocket = false;
    bool closing = false;          // close once the output is written
    bool draining = false;         // output done and our side shut: reading until the peer closes
    Clock::time_point drain_until;
    Clock::time_point close_by{};  // closing: when its last bytes have to be gone
    bool dead = false;             // to be removed by reap()
    bool writable_wait = false;    // registered for EPOLLOUT
    uint64_t sent = 0;
    Clock::time_point last_input = Clock::now(), last_ping = Clock::now();

    // permessage-deflate
    bool deflate = false, reset_deflater = false;
    z_stream deflater{}, inflater{};
    std::string message;           // fragments of the message being received
    int message_opcode = 0;
    bool message_compressed = false;

    ~Connection()
    {
        if (deflate) {
            deflateEnd(&deflater);
            inflateEnd(&inflater);
        }
    }
    size_t pending() const { return output.size() - output_at; }
};

WebServer::WebServer(const std::string &listen, const std::string &web_dir, Handler handler, Access access)
    : handler_(std::move(handler)), web_dir_(web_dir), access_(std::move(access))
{
    for (size_t i = 0; i < kWebFileCount; ++i) {
        auto asset = std::make_unique<Asset>();
        asset->path = kWebFiles[i].path;
        asset->type = content_type(asset->path);
        asset->body.assign(reinterpret_cast<const char *>(kWebFiles[i].data), kWebFiles[i].size);
        asset->gzipped = gzip(asset->body);
        asset->tag = etag(asset->body);
        assets_.push_back(std::move(asset));
    }

    std::string host = "0.0.0.0", port = listen;
    if (size_t colon = listen.rfind(':'); colon != std::string::npos) {
        host = listen.substr(0, colon);
        port = listen.substr(colon + 1);
        if (host.size() >= 2 && host.front() == '[' && host.back() == ']') host = host.substr(1, host.size() - 2);
    }
    addrinfo hints{}, *found = nullptr;
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_PASSIVE | AI_NUMERICSERV;
    if (int error = getaddrinfo(host.c_str(), port.c_str(), &hints, &found))
        throw std::runtime_error("cannot listen on " + listen + ": " + gai_strerror(error));
    for (addrinfo *a = found; a && listener_ < 0; a = a->ai_next) {
        int fd = socket(a->ai_family, a->ai_socktype | SOCK_CLOEXEC, a->ai_protocol);
        if (fd < 0) continue;
        int yes = 1;
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
        if (bind(fd, a->ai_addr, a->ai_addrlen) == 0 && ::listen(fd, 64) == 0) {
            listener_ = fd;
            sockaddr_storage bound{};
            socklen_t length = sizeof(bound);
            getsockname(fd, reinterpret_cast<sockaddr *>(&bound), &length);  // the port, if 0 was asked for
            char name[NI_MAXHOST], service[NI_MAXSERV];
            getnameinfo(reinterpret_cast<sockaddr *>(&bound), length, name, sizeof(name), service, sizeof(service),
                        NI_NUMERICHOST | NI_NUMERICSERV);
            address_ = (a->ai_family == AF_INET6 ? "[" + std::string(name) + "]" : std::string(name)) + ":" + service;
            port_ = service;
            const std::string literal = a->ai_family == AF_INET6 ? "[" + std::string(name) + "]" : std::string(name);
            bound_ = literal == "0.0.0.0" || literal == "[::]" ? std::string() : literal;
        } else {
            ::close(fd);
        }
    }
    freeaddrinfo(found);
    if (listener_ < 0) throw std::runtime_error("cannot listen on " + listen + ": " + std::strerror(errno));
    set_nonblocking(listener_);
    learn_names();
    const bool loopback = bound_ == "[::1]" || bound_.compare(0, 4, "127.") == 0;
    // A token given is wanted wherever the server listens; without one, a token is made once
    // the server can be reached from beyond this machine.
    if (access_.token && !access_.token_value.empty()) token_ = access_.token_value;
    else if (access_.token && !loopback) token_ = random_token();

    epoll_ = epoll_create1(EPOLL_CLOEXEC);
    event_ = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    epoll_event ev{};
    ev.events = EPOLLIN;
    ev.data.u64 = 0;
    epoll_ctl(epoll_, EPOLL_CTL_ADD, listener_, &ev);
    ev.data.u64 = ~0ull;
    epoll_ctl(epoll_, EPOLL_CTL_ADD, event_, &ev);
}

// The names this server is asked for by: the address it listens on, or with all of them every
// address this machine has now; the machine's name, as it is and as mDNS gives it; localhost;
// and the names it was told. Looked up again when a name is not among them (an interface may
// have come since), at most once a second.
void WebServer::learn_names()
{
    names_.clear();
    names_at_ = Clock::now();
    for (const char *local : {"localhost", "127.0.0.1", "[::1]"}) names_.insert(local);
    for (const auto &given : access_.hosts) {
        std::string name, port;
        if (split_host(given, name, port)) names_.insert(name);
    }
    char machine[256] = "";
    if (gethostname(machine, sizeof(machine) - 1) == 0 && machine[0]) {
        const std::string name = lower(machine), bare = name.substr(0, name.find('.'));
        names_.insert(name);
        names_.insert(bare);
        names_.insert(bare + ".local");
    }
    if (!bound_.empty()) {
        names_.insert(bound_);
        return;
    }
    ifaddrs *all = nullptr;
    if (getifaddrs(&all)) return;
    for (ifaddrs *one = all; one; one = one->ifa_next) {
        if (!one->ifa_addr) continue;
        const int family = one->ifa_addr->sa_family;
        if (family != AF_INET && family != AF_INET6) continue;
        char text[NI_MAXHOST];
        if (getnameinfo(one->ifa_addr, family == AF_INET ? sizeof(sockaddr_in) : sizeof(sockaddr_in6), text, sizeof(text),
                        nullptr, 0, NI_NUMERICHOST))
            continue;
        std::string address = lower(text);
        address.resize(address.find('%') == std::string::npos ? address.size() : address.find('%'));  // a link's scope
        names_.insert(family == AF_INET6 ? "[" + address + "]" : address);
    }
    freeifaddrs(all);
}

bool WebServer::answers_to(const std::string &host)
{
    std::string name, port;
    if (!split_host(host, name, port)) return false;
    if (!access_.behind_proxy && port != port_ && !(port.empty() && port_ == "80")) return false;
    if (names_.count(name)) return true;
    if (Clock::now() - names_at_ < 1s) return false;
    learn_names();
    return names_.count(name) != 0;
}

// A browser's Origin, "scheme://host[:port]": this server under one of its names, or an origin
// it was told of.
bool WebServer::origin_allowed(std::string origin)
{
    origin = lower(trim(origin));
    while (!origin.empty() && origin.back() == '/') origin.pop_back();
    for (const auto &given : access_.origins) {
        std::string known = lower(trim(given));
        while (!known.empty() && known.back() == '/') known.pop_back();
        if (origin == known) return true;
    }
    const size_t scheme = origin.find("://");
    if (scheme == std::string::npos) return false;
    std::string host = origin.substr(scheme + 3), name, port;
    if (!split_host(host, name, port)) return false;
    if (port.empty()) host += origin.compare(0, scheme, "https") == 0 ? ":443" : ":80";
    return answers_to(host);
}

bool WebServer::authorized(const std::map<std::string, std::string> &headers, const std::string &query) const
{
    if (token_.empty()) return true;
    auto header = [&](const char *name) {
        auto it = headers.find(name);
        return it == headers.end() ? std::string() : it->second;
    };
    return same_secret(field(query, '&', "token"), token_) ||
           same_secret(field(header("cookie"), ';', "w1700k-" + port_), token_) ||
           same_secret(header("authorization"), "Bearer " + token_);
}

std::vector<std::string> WebServer::names()
{
    learn_names();
    return {names_.begin(), names_.end()};
}

// An address to open: the one listened on, or with all of them one of this machine's that is
// not loopback (a v4 one before a v6 one), with the token when one is wanted.
std::string WebServer::link()
{
    std::string host = bound_;
    if (host.empty()) {
        learn_names();
        for (const auto &name : names_) {
            const bool address = name.find_first_not_of("0123456789.") == std::string::npos || name[0] == '[';
            const bool local = name == "[::1]" || name.compare(0, 4, "127.") == 0 || name.compare(0, 5, "[fe80") == 0;
            if (address && !local && (host.empty() || (host[0] == '[' && name[0] != '['))) host = name;
        }
        if (host.empty()) host = "127.0.0.1";
    }
    return "http://" + host + ":" + port_ + "/" + (token_.empty() ? "" : "?token=" + token_);
}

WebServer::~WebServer()
{
    connections_.clear();
    if (listener_ >= 0) ::close(listener_);
    if (event_ >= 0) ::close(event_);
    if (epoll_ >= 0) ::close(epoll_);
}

void WebServer::wake()
{
    uint64_t one = 1;
    if (write(event_, &one, sizeof(one)) < 0) {
        // The counter is saturated only if nobody reads it; nothing to do.
    }
}

void WebServer::run(const std::atomic<bool> &stop)
{
    epoll_event events[64];
    auto last_tick = Clock::now();
    while (!stop) {
        int n = epoll_wait(epoll_, events, 64, 20);
        if (n < 0 && errno != EINTR) throw std::runtime_error("epoll failed");
        for (int i = 0; i < n; ++i) {
            uint64_t key = events[i].data.u64;
            if (key == 0) {
                accept_clients();
            } else if (key == ~0ull) {
                uint64_t count;
                while (read(event_, &count, sizeof(count)) > 0) {
                }
                if (handler_.wake) handler_.wake();
            } else if (Connection *c = find(unsigned(key))) {
                if (events[i].events & (EPOLLERR | EPOLLHUP)) c->dead = true;
                if (!c->dead && (events[i].events & EPOLLIN)) receive(*c);
                if (!c->dead && (events[i].events & EPOLLOUT)) flush(*c);
            }
        }
        reap();
        auto now = Clock::now();
        if (now - last_tick >= 20ms) {
            last_tick = now;
            if (handler_.tick) handler_.tick();
            for (auto &entry : connections_) {
                Connection &c = *entry.second;
                if (c.dead) continue;
                if (c.closing && !c.draining && c.close_by == Clock::time_point{}) c.close_by = now + kCloseWithin;
                if (c.draining ? now > c.drain_until
                               : (c.closing && now > c.close_by) || now - c.last_input > (c.websocket ? kIdleTimeout : 15s)) {
                    c.dead = true;
                } else if (c.websocket && !c.closing && now - c.last_ping > kPingInterval) {
                    c.last_ping = now;
                    send_frame(c, 9, "", false);
                }
            }
            reap();
            if (accept_paused_ && now >= accept_again_) {
                accept_paused_ = false;
                epoll_event ev{};
                ev.events = EPOLLIN;
                ev.data.u64 = 0;
                epoll_ctl(epoll_, EPOLL_CTL_ADD, listener_, &ev);
            }
        }
    }
}

WebServer::Connection *WebServer::find(unsigned client) const
{
    auto it = connections_.find(client);
    return it == connections_.end() || it->second->dead ? nullptr : it->second.get();
}

void WebServer::accept_clients()
{
    for (;;) {
        sockaddr_storage address{};
        socklen_t length = sizeof(address);
        int fd = accept4(listener_, reinterpret_cast<sockaddr *>(&address), &length, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0) {
            // No descriptor or no memory for it: the connection stays in the queue and the
            // listener stays readable, so it is left out of the wait for a moment rather than
            // asked again at once, forever.
            if (errno == EMFILE || errno == ENFILE || errno == ENOBUFS || errno == ENOMEM) {
                epoll_ctl(epoll_, EPOLL_CTL_DEL, listener_, nullptr);
                accept_paused_ = true;
                accept_again_ = Clock::now() + kAcceptPause;
            }
            return;
        }
        if (connections_.size() >= kMaxConnections) {  // a page holds two or three
            ::close(fd);
            continue;
        }
        int yes = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
        // A small send buffer keeps frames queued here, where they can still
        // be merged, rather than in the kernel on a slow link.
        int buffer = 128 * 1024;
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
        auto c = std::make_unique<Connection>();
        c->id = next_id_++;
        c->fd = fd;
        char name[NI_MAXHOST];
        if (getnameinfo(reinterpret_cast<sockaddr *>(&address), length, name, sizeof(name), nullptr, 0,
                        NI_NUMERICHOST) == 0)
            c->peer = name;
        epoll_event ev{};
        ev.events = EPOLLIN | EPOLLRDHUP;
        ev.data.u64 = c->id;
        epoll_ctl(epoll_, EPOLL_CTL_ADD, fd, &ev);
        connections_[c->id] = std::move(c);
    }
}

void WebServer::receive(Connection &c)
{
    char buffer[16384];
    bool ended = false;
    for (;;) {
        ssize_t n = read(c.fd, buffer, sizeof(buffer));
        if (n > 0) {
            if (!c.draining && !c.closing) c.input.append(buffer, size_t(n));  // a closing one is heard no more
            c.last_input = Clock::now();
            if (c.input.size() > kMaxMessage + kMaxRequestHeader + 16) break;
            continue;
        }
        if (n == 0 || (errno != EAGAIN && errno != EINTR)) {
            ended = true;
            break;
        }
        if (errno == EAGAIN) break;
    }
    // What arrived with the end of the connection still counts.
    if (!c.draining && !c.websocket) handle_http(c);
    if (!c.draining && !c.dead && c.websocket) handle_frames(c);
    if (ended) c.dead = true;
}

void WebServer::flush(Connection &c)
{
    while (c.pending()) {
        ssize_t n = send(c.fd, c.output.data() + c.output_at, c.pending(), MSG_NOSIGNAL);
        if (n > 0) {
            c.output_at += size_t(n);
            c.sent += uint64_t(n);
        } else if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
            break;
        } else {
            c.dead = true;
            return;
        }
    }
    if (!c.pending()) {
        c.output.clear();
        c.output_at = 0;
        // Closing: shut our side and read until the peer closes too. Closing
        // the socket with its data unread would reset the connection, and
        // the peer could lose what was just sent (such as a close frame).
        if (c.closing && !c.draining) {
            shutdown(c.fd, SHUT_WR);
            c.draining = true;
            c.drain_until = Clock::now() + 2s;
        }
    } else if (c.output_at > 1 << 20) {
        c.output.erase(0, c.output_at);
        c.output_at = 0;
    }
    bool want = c.pending() != 0;
    if (want != c.writable_wait) {
        c.writable_wait = want;
        epoll_event ev{};
        ev.events = EPOLLIN | EPOLLRDHUP | (want ? uint32_t(EPOLLOUT) : 0u);
        ev.data.u64 = c.id;
        epoll_ctl(epoll_, EPOLL_CTL_MOD, c.fd, &ev);
    }
}

void WebServer::reap()
{
    std::vector<unsigned> closed;
    for (auto it = connections_.begin(); it != connections_.end();) {
        if (!it->second->dead) {
            ++it;
            continue;
        }
        epoll_ctl(epoll_, EPOLL_CTL_DEL, it->second->fd, nullptr);
        ::close(it->second->fd);
        if (it->second->websocket) closed.push_back(it->first);
        it = connections_.erase(it);
    }
    for (unsigned client : closed)
        if (handler_.close) handler_.close(client);
}

void WebServer::close(unsigned client, uint16_t code, const std::string &reason)
{
    if (Connection *c = find(client)) {
        std::string payload = {char(code >> 8), char(code & 255)};
        send_frame(*c, 8, payload + reason.substr(0, 123), false);
        c->closing = true;
        flush(*c);
    }
}

// ---- HTTP -------------------------------------------------------------------------------------

void WebServer::refuse(Connection &c, const char *status, const std::string &why)
{
    c.output += std::string("HTTP/1.1 ") + status + "\r\nContent-Type: text/plain; charset=utf-8\r\n"
                "Cache-Control: no-store\r\nContent-Length: " + std::to_string(why.size()) + "\r\nConnection: close\r\n\r\n" + why;
    c.closing = true;
}

void WebServer::handle_http(Connection &c)
{
    while (!c.websocket && !c.closing && !c.dead) {
        if (c.pending() > kHttpBacklog) {  // asking faster than taking: no more is answered
            c.closing = true;
            c.input.clear();
            flush(c);
            return;
        }
        size_t end = c.input.find("\r\n\r\n");
        if (end == std::string::npos) {
            if (c.input.size() > kMaxRequestHeader) {
                c.output += "HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
                c.closing = true;
                flush(c);
            }
            return;
        }
        std::string head = c.input.substr(0, end);
        c.input.erase(0, end + 4);

        std::istringstream lines(head);
        std::string line, method, target, version;
        std::getline(lines, line);
        std::istringstream(line) >> method >> target >> version;
        std::map<std::string, std::string> headers;
        while (std::getline(lines, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            size_t colon = line.find(':');
            if (colon == std::string::npos) continue;
            std::string name = lower(trim(line.substr(0, colon))), value = trim(line.substr(colon + 1));
            headers[name] = headers.count(name) ? headers[name] + ", " + value : value;
        }
        std::string path = target.substr(0, target.find('?'));
        bool keep_alive = version == "HTTP/1.1" ? !has_token(headers["connection"], "close")
                                                : has_token(headers["connection"], "keep-alive");
        if (headers.count("content-length") && headers["content-length"] != "0") keep_alive = false;

        const std::string query = target.find('?') == std::string::npos ? std::string() : target.substr(target.find('?') + 1);
        const std::string asked = access_.behind_proxy && headers.count("x-forwarded-host") ? headers["x-forwarded-host"]
                                                                                             : headers["host"];
        if (!answers_to(asked)) {
            // Not said: which names it does answer to. A page that got here under a name of its
            // own could read that.
            refuse(c, "421 Misdirected Request",
                   "This spectrum server is not known by the name it was asked for.\n"
                   "It answers to its own addresses, this machine's name and localhost; start it with\n"
                   "--host NAME to add a name (it lists the names it answers to when it starts).\n");
        } else if (!authorized(headers, query)) {
            refuse(c, "401 Unauthorized",
                   "This spectrum server wants its access token.\n"
                   "Open the link it printed when it started: the one that ends in ?token=...\n");
        } else if (path == "/ws") {
            if (upgrade(c, headers)) return;
            c.closing = true;
        } else if (!token_.empty() && !field(query, '&', "token").empty()) {
            // The link that was printed: the token leaves the address for a cookie, so that it is
            // not kept in the history or shown on a shared screen, and the page's own requests
            // and its socket carry it.
            const bool local = path.size() >= 1 && path[0] == '/' && path.compare(0, 2, "//") != 0;
            c.output += "HTTP/1.1 303 See Other\r\nLocation: " + (local ? path : std::string("/")) + "\r\nSet-Cookie: w1700k-" +
                        port_ + "=" + token_ + "; Path=/; HttpOnly; SameSite=Strict\r\nCache-Control: no-store\r\n"
                        "Content-Length: 0\r\n\r\n";
            if (!keep_alive) c.closing = true;
        } else {
            respond(c, method, path, headers);
            if (!keep_alive) c.closing = true;
        }
        flush(c);
    }
}

void WebServer::respond(Connection &c, const std::string &method, const std::string &path,
                        const std::map<std::string, std::string> &headers)
{
    auto header = [&](const char *name) {
        auto it = headers.find(name);
        return it == headers.end() ? std::string() : it->second;
    };
    if (method != "GET" && method != "HEAD") {
        c.output += "HTTP/1.1 405 Method Not Allowed\r\nAllow: GET, HEAD\r\nContent-Length: 0\r\n\r\n";
        return;
    }
    std::string name = path == "/" ? "/index.html" : path == "/favicon.ico" ? "/icon.svg" : path;

    Asset loaded;
    const Asset *asset = nullptr;
    if (!web_dir_.empty()) {
        // Development: read the file on every request.
        if (!name.empty() && name[0] == '/' && name.find("..") == std::string::npos && name.find('/', 1) == std::string::npos) {
            std::ifstream file(web_dir_ + name, std::ios::binary);
            if (file) {
                loaded.path = name;
                loaded.type = content_type(name);
                loaded.body.assign(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
                loaded.tag = etag(loaded.body);
                asset = &loaded;
            }
        }
    } else {
        for (const auto &a : assets_)
            if (a->path == name) asset = a.get();
    }
    if (!asset) {
        const char *body = "not found\n";
        c.output += "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: " +
                    std::to_string(std::strlen(body)) + "\r\n\r\n" + (method == "GET" ? body : "");
        return;
    }

    std::string common = "Content-Type: " + asset->type + "\r\nCache-Control: no-cache\r\nETag: " + asset->tag +
                         "\r\nVary: Accept-Encoding\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
                         "Content-Security-Policy: default-src 'self'; connect-src 'self' ws: wss:; "
                         "img-src 'self' data:; frame-ancestors 'none'\r\n";
    if (header("if-none-match") == asset->tag) {
        c.output += "HTTP/1.1 304 Not Modified\r\n" + common + "\r\n";
        return;
    }
    bool zipped = !asset->gzipped.empty() && header("accept-encoding").find("gzip") != std::string::npos;
    const std::string &body = zipped ? asset->gzipped : asset->body;
    c.output += "HTTP/1.1 200 OK\r\n" + common + (zipped ? "Content-Encoding: gzip\r\n" : "") +
                "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n";
    if (method == "GET") c.output += body;
}

bool WebServer::upgrade(Connection &c, const std::map<std::string, std::string> &headers)
{
    auto header = [&](const char *name) {
        auto it = headers.find(name);
        return it == headers.end() ? std::string() : it->second;
    };
    std::string key = header("sec-websocket-key");
    if (!has_token(header("upgrade"), "websocket") || !has_token(header("connection"), "upgrade") ||
        header("sec-websocket-version") != "13" || key.empty()) {
        c.output += "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        return false;
    }
    // A page from another site must not drive the receiver from the viewer's browser: a
    // browser's Origin has to be this server under one of its names (not merely equal to the
    // Host it sent, which a page reached by a name of its own also is), or an origin named.
    if (std::string origin = header("origin"); !origin.empty() && !origin_allowed(origin)) {
        c.output += "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        return false;
    }

    std::string extension;
    for (const auto &offer : split(header("sec-websocket-extensions"), ',')) {
        auto params = split(offer, ';');
        if (params.empty() || lower(params[0]) != "permessage-deflate") continue;
        int window = 15;
        bool reset = false, acceptable = true;
        for (size_t i = 1; i < params.size(); ++i) {
            std::string p = lower(params[i]);
            if (p == "server_no_context_takeover") reset = true;
            else if (p.compare(0, 23, "server_max_window_bits=") == 0) window = std::atoi(p.c_str() + 23);
            else if (p != "client_no_context_takeover" && p.compare(0, 22, "client_max_window_bits") != 0)
                acceptable = false;
        }
        if (!acceptable || window < 9 || window > 15) continue;
        c.deflate = true;
        c.reset_deflater = reset;
        deflateInit2(&c.deflater, 3, Z_DEFLATED, -window, 8, Z_DEFAULT_STRATEGY);
        inflateInit2(&c.inflater, -15);
        extension = "permessage-deflate; client_no_context_takeover";
        if (reset) extension += "; server_no_context_takeover";
        if (window != 15) extension += "; server_max_window_bits=" + std::to_string(window);
        break;
    }

    auto digest = sha1(key + kWebSocketGuid);
    c.output += "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: " + base64(digest.data(), digest.size()) + "\r\n" +
                (extension.empty() ? "" : "Sec-WebSocket-Extensions: " + extension + "\r\n") + "\r\n";
    c.websocket = true;
    c.last_ping = Clock::now();
    flush(c);
    if (!c.dead && handler_.open) handler_.open(c.id);
    return true;
}

// ---- WebSocket ------------------------------------------------------------------------------------

void WebServer::handle_frames(Connection &c)
{
    const unsigned id = c.id;
    auto fail = [&](uint16_t code) {
        std::string payload = {char(code >> 8), char(code & 255)};
        send_frame(c, 8, payload, false);
        c.closing = true;
        c.input.clear();
        flush(c);
    };
    while (c.input.size() >= 2 && !c.closing && !c.dead) {
        const uint8_t *p = reinterpret_cast<const uint8_t *>(c.input.data());
        bool fin = p[0] & 0x80, rsv1 = p[0] & 0x40;
        int opcode = p[0] & 0x0F;
        uint64_t length = p[1] & 0x7F;
        size_t header = 2;
        if (!(p[1] & 0x80) || (p[0] & 0x30) || (rsv1 && !c.deflate)) return fail(1002);
        if (length == 126) {
            if (c.input.size() < 4) return;
            length = uint64_t(p[2]) << 8 | p[3];
            header = 4;
        } else if (length == 127) {
            if (c.input.size() < 10) return;
            length = 0;
            for (int i = 0; i < 8; ++i) length = length << 8 | p[2 + i];
            header = 10;
        }
        if (length > kMaxMessage) return fail(1009);
        if (c.input.size() < header + 4 + length) return;
        const uint8_t *mask = p + header;
        std::string payload(size_t(length), '\0');
        for (size_t i = 0; i < length; ++i) payload[i] = char(p[header + 4 + i] ^ mask[i % 4]);
        c.input.erase(0, header + 4 + size_t(length));

        if (opcode >= 8) {
            if (!fin || length > 125) return fail(1002);
            if (opcode == 8) {
                send_frame(c, 8, payload.substr(0, 2), false);
                c.closing = true;
                flush(c);
                return;
            }
            if (opcode == 9) send_frame(c, 10, payload, false);
            continue;
        }
        if (opcode != 0) {
            if (!c.message.empty() || c.message_opcode) return fail(1002);
            c.message_opcode = opcode;
            c.message_compressed = rsv1;
        } else if (!c.message_opcode) {
            return fail(1002);
        }
        c.message += payload;
        if (c.message.size() > kMaxMessage) return fail(1009);
        if (!fin) continue;

        std::string message;
        message.swap(c.message);
        int kind = c.message_opcode;
        c.message_opcode = 0;
        if (c.message_compressed) {
            message.append("\x00\x00\xff\xff", 4);
            std::string out;
            char buffer[16384];
            c.inflater.next_in = (Bytef *)message.data();
            c.inflater.avail_in = uInt(message.size());
            int status = Z_OK;
            while (c.inflater.avail_in && status == Z_OK) {
                c.inflater.next_out = (Bytef *)buffer;
                c.inflater.avail_out = sizeof(buffer);
                status = inflate(&c.inflater, Z_SYNC_FLUSH);
                out.append(buffer, sizeof(buffer) - c.inflater.avail_out);
                if (out.size() > kMaxMessage) return fail(1009);
            }
            inflateReset(&c.inflater);
            if (status != Z_OK && status != Z_BUF_ERROR) return fail(1007);
            message.swap(out);
        }
        if (kind == 1 && handler_.text) handler_.text(id, message);
    }
}

void WebServer::send_frame(Connection &c, int opcode, const std::string &payload, bool compress)
{
    std::string body;
    bool compressed = compress && c.deflate && payload.size() >= kMinCompressed;
    if (compressed) {
        body.resize(deflateBound(&c.deflater, uLong(payload.size())) + 16);
        c.deflater.next_in = (Bytef *)payload.data();
        c.deflater.avail_in = uInt(payload.size());
        c.deflater.next_out = (Bytef *)&body[0];
        c.deflater.avail_out = uInt(body.size());
        size_t before = c.deflater.total_out;
        deflate(&c.deflater, Z_SYNC_FLUSH);
        body.resize(c.deflater.total_out - before);
        if (body.size() >= 4) body.resize(body.size() - 4);  // the flush's 00 00 FF FF
        if (c.reset_deflater) deflateReset(&c.deflater);
    }
    const std::string &data = compressed ? body : payload;
    std::string header(1, char(0x80 | (compressed ? 0x40 : 0) | opcode));
    if (data.size() < 126) {
        header += char(data.size());
    } else if (data.size() < 65536) {
        header += char(126);
        header += char(data.size() >> 8);
        header += char(data.size() & 255);
    } else {
        header += char(127);
        for (int i = 7; i >= 0; --i) header += char(uint64_t(data.size()) >> (8 * i));
    }
    c.output += header;
    c.output += data;
    flush(c);
}

void WebServer::send_text(unsigned client, const std::string &text)
{
    if (Connection *c = find(client); c && c->websocket && !c->closing) send_frame(*c, 1, text, true);
}

void WebServer::send_binary(unsigned client, const std::string &data)
{
    if (Connection *c = find(client); c && c->websocket && !c->closing) send_frame(*c, 2, data, true);
}

size_t WebServer::queued(unsigned client) const
{
    Connection *c = find(client);
    return c ? c->pending() : 0;
}

uint64_t WebServer::sent(unsigned client) const
{
    Connection *c = find(client);
    return c ? c->sent : 0;
}

bool WebServer::compressing(unsigned client) const
{
    Connection *c = find(client);
    return c && c->deflate;
}

std::string WebServer::peer(unsigned client) const
{
    Connection *c = find(client);
    return c ? c->peer : "";
}
