// SPDX-License-Identifier: MIT
// A small HTTP/1.1 and WebSocket server (RFC 6455, with permessage-deflate,
// RFC 7692) on one epoll thread: static files, and WebSocket clients at /ws.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <chrono>
#include <memory>
#include <set>
#include <string>
#include <vector>

// A file of the web UI compiled into the tool (build/web_assets.cpp).
struct EmbeddedFile {
    const char *path;  // "/index.html"
    const unsigned char *data;
    size_t size;
};
extern const EmbeddedFile kWebFiles[];
extern const size_t kWebFileCount;

class WebServer {
public:
    struct Handler {
        std::function<void(unsigned client)> open;
        std::function<void(unsigned client, const std::string &message)> text;
        std::function<void(unsigned client)> close;
        std::function<void()> wake;  // after wake() was called on any thread
        std::function<void()> tick;  // every 20 ms or so
    };

    // Who is answered. The page drives a receiver, so a request has to be meant for this server
    // and, where it listens beyond its own machine, has to come from someone given its link.
    //
    //  - The name: a request's Host has to be one this server knows itself by: an address it
    //    listens on, this machine's name, localhost, or a name given here. A page on another
    //    site whose name was made to resolve to this machine (DNS rebinding) passes a browser's
    //    same-origin rule, and its Origin equals its Host; its Host is still its own name.
    //  - The socket's Origin, when a browser sends one, has to be such a name too, or one of
    //    `origins` (a front end served from somewhere else).
    //  - The token: wanted with every request once the server listens on more than loopback.
    //    It comes as ?token= (the link the server prints; a page is then redirected to the same
    //    address without it, the token in a cookie), as that cookie, or as "Authorization:
    //    Bearer".
    struct Access {
        std::vector<std::string> hosts;    // names answered to beside the server's own (--host)
        std::vector<std::string> origins;  // other origins whose pages may open the socket (--allow-origin)
        bool behind_proxy;                 // the name is a proxy's X-Forwarded-Host, on whatever port
        bool token;                        // want the token when listening beyond this machine
        std::string token_value;           // that token; empty: one is made
    };

    // listen: "ADDRESS:PORT" or "PORT". web_dir: serve files from there,
    // read on every request, instead of the built-in copy.
    WebServer(const std::string &listen, const std::string &web_dir, Handler handler, Access access);
    ~WebServer();
    WebServer(const WebServer &) = delete;
    WebServer &operator=(const WebServer &) = delete;

    const std::string &address() const { return address_; }
    const std::string &token() const { return token_; }  // empty: none is wanted
    std::string link();                                   // an address to open, the token in it
    std::vector<std::string> names();                     // the names answered to
    // Runs the event loop on the calling thread until `stop` is set.
    void run(const std::atomic<bool> &stop);
    // Wakes the loop, which then calls Handler::wake. Any thread.
    void wake();

    // The rest is for the loop thread (inside the handler callbacks).
    void send_text(unsigned client, const std::string &text);
    void send_binary(unsigned client, const std::string &data);
    size_t queued(unsigned client) const;       // bytes the socket has not taken yet
    uint64_t sent(unsigned client) const;       // bytes on the wire so far
    bool compressing(unsigned client) const;    // permessage-deflate in use
    std::string peer(unsigned client) const;
    void close(unsigned client, uint16_t code, const std::string &reason);

private:
    struct Asset;
    struct Connection;

    void accept_clients();
    void receive(Connection &c);
    void flush(Connection &c);
    void handle_http(Connection &c);
    bool upgrade(Connection &c, const std::map<std::string, std::string> &headers);
    void respond(Connection &c, const std::string &method, const std::string &path,
                 const std::map<std::string, std::string> &headers);
    void handle_frames(Connection &c);
    void send_frame(Connection &c, int opcode, const std::string &payload, bool compress);
    void reap();  // removes dead connections; safe only between events
    Connection *find(unsigned client) const;
    void refuse(Connection &c, const char *status, const std::string &why);
    bool answers_to(const std::string &host);
    bool origin_allowed(std::string origin);
    bool authorized(const std::map<std::string, std::string> &headers, const std::string &query) const;
    void learn_names();

    Handler handler_;
    std::string web_dir_;
    std::vector<std::unique_ptr<Asset>> assets_;
    std::string address_;
    Access access_;
    std::string token_, port_, bound_;  // bound_: the one address listened on; empty: all of them
    std::set<std::string> names_;
    std::chrono::steady_clock::time_point names_at_{}, accept_again_{};
    bool accept_paused_ = false;
    int listener_ = -1, epoll_ = -1, event_ = -1;
    unsigned next_id_ = 1;
    std::map<unsigned, std::unique_ptr<Connection>> connections_;
};
