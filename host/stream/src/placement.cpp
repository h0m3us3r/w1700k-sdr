// SPDX-License-Identifier: MIT
#include "placement.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>

#include <arpa/inet.h>
#include <dirent.h>
#include <ifaddrs.h>
#include <limits.h>
#include <net/if.h>
#include <sched.h>
#include <sys/stat.h>
#include <sys/types.h>

namespace placement {
namespace {

bool exists(const std::string &path)
{
    struct stat st;
    return stat(path.c_str(), &st) == 0;
}

std::string first_line(const std::string &path)
{
    std::ifstream f(path);
    std::string line;
    std::getline(f, line);
    return line;
}

// The lowest-numbered entry of a directory of numbers; -1: none.
long lowest(const std::string &directory)
{
    long best = -1;
    if (DIR *d = opendir(directory.c_str())) {
        while (dirent *e = readdir(d)) {
            char *end;
            const long n = std::strtol(e->d_name, &end, 10);
            if (end != e->d_name && !*end && (best < 0 || n < best)) best = n;
        }
        closedir(d);
    }
    return best;
}

// Whether `name` is a USB bus as sysfs names its root hub: usb1, usb2, ...
bool is_bus(const char *name)
{
    return !std::strncmp(name, "usb", 3) && name[3] && std::strspn(name + 3, "0123456789") == std::strlen(name + 3);
}

// The interrupt /proc/interrupts lists under USB bus `bus` ("usb1": "xhci-hcd:usb1"); -1: none.
long bus_interrupt(const std::string &bus, const std::string &proc)
{
    std::ifstream f(proc + "/interrupts");
    const std::string wanted = ":" + bus;
    for (std::string line; std::getline(f, line);) {
        std::istringstream words(line);
        std::string number, word;
        words >> number;
        while (words >> word) {
            if (word.back() == ',') word.pop_back();
            if (word.size() <= wanted.size() || word.compare(word.size() - wanted.size(), wanted.size(), wanted)) continue;
            char *end;
            const long irq = std::strtol(number.c_str(), &end, 10);
            if (end != number.c_str() && !std::strcmp(end, ":")) return irq;
        }
    }
    return -1;
}

// A USB controller that is no PCI device shows no interrupt in sysfs: it is listed under the
// name of one of the controller's buses, not always the one the adapter is on.
long controller_interrupt(const std::string &root_hub, const std::string &proc)
{
    const size_t slash = root_hub.rfind('/');
    if (slash == std::string::npos || !is_bus(root_hub.c_str() + slash + 1)) return -1;
    long irq = bus_interrupt(root_hub.substr(slash + 1), proc);
    if (DIR *d = irq < 0 ? opendir(root_hub.substr(0, slash).c_str()) : nullptr) {
        while (dirent *e = irq < 0 ? readdir(d) : nullptr)
            if (is_bus(e->d_name)) irq = bus_interrupt(e->d_name, proc);
        closedir(d);
    }
    return irq;
}

}  // namespace

int receive_cpu(const std::string &interface, const std::string &sys, const std::string &proc)
{
    const std::string net = sys + "/class/net/" + interface;
    if (interface.empty() || !exists(net + "/queues/rx-0") || exists(net + "/queues/rx-1")) return -1;
    char resolved[PATH_MAX];
    if (!realpath((net + "/device").c_str(), resolved)) return -1;
    // The interface's own interrupt, or that of the nearest device above it that has one (the
    // USB controller of an adapter).
    long irq = -1;
    for (std::string device = resolved; device.size() > sys.size() + 1 && irq < 0;
         device.erase(device.rfind('/'))) {
        if (exists(device + "/msi_irqs")) {
            irq = lowest(device + "/msi_irqs");
        } else {
            const std::string line = first_line(device + "/irq");
            if (!line.empty() && std::atol(line.c_str()) > 0) irq = std::atol(line.c_str());
            else irq = controller_interrupt(device, proc);
        }
    }
    if (irq < 0) return -1;
    const std::string cpus = first_line(proc + "/irq/" + std::to_string(irq) + "/effective_affinity_list");
    if (cpus.empty() || cpus.find_first_not_of("0123456789") != std::string::npos) return -1;
    return std::atoi(cpus.c_str());
}

std::string interface_of(const std::string &ip)
{
    in_addr wanted;
    ifaddrs *list = nullptr;
    std::string name;
    if (inet_pton(AF_INET, ip.c_str(), &wanted) != 1 || getifaddrs(&list)) return name;
    for (ifaddrs *a = list; a; a = a->ifa_next)
        if (a->ifa_addr && a->ifa_addr->sa_family == AF_INET &&
            reinterpret_cast<sockaddr_in *>(a->ifa_addr)->sin_addr.s_addr == wanted.s_addr) {
            name = a->ifa_name;
            break;
        }
    freeifaddrs(list);
    return name;
}

void keep_off(int cpu)
{
    static cpu_set_t started;
    static bool known = false;
    if (!known) {
        if (sched_getaffinity(0, sizeof(started), &started)) return;
        known = true;
    }
    cpu_set_t set = started;
    if (cpu >= 0 && cpu < CPU_SETSIZE && CPU_ISSET(cpu, &set)) {
        if (CPU_COUNT(&set) < 4) return;
        CPU_CLR(cpu, &set);
    }
    if (DIR *d = opendir("/proc/self/task")) {
        while (dirent *e = readdir(d))
            if (const long tid = std::atol(e->d_name)) sched_setaffinity(pid_t(tid), sizeof(set), &set);
        closedir(d);
    }
}

}  // namespace placement
