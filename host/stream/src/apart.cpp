// SPDX-License-Identifier: MIT
#include "apart.h"

#include <cerrno>

#include <fcntl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

bool apart::run(const std::vector<std::string> &arguments, int nice_level)
{
    char program[4096];  // this program's own file, by its name: so the process is listed under it
    const ssize_t length = readlink("/proc/self/exe", program, sizeof program - 1);
    if (length <= 0) return false;
    program[length] = 0;
    const char *base = program;
    for (const char *c = program; *c; ++c)
        if (*c == '/') base = c + 1;
    std::vector<char *> argv{const_cast<char *>(base)};
    for (const std::string &argument : arguments) argv.push_back(const_cast<char *>(argument.c_str()));
    argv.push_back(nullptr);
    const long limit = sysconf(_SC_OPEN_MAX);
    const int files = limit > 0 && limit < 65536 ? int(limit) : 65536;

    // A child that starts the process and ends, so the process is nobody's to wait for. From the
    // first fork on (other threads of this process are not in the child) only what a process
    // may do between fork and exec.
    const pid_t child = fork();
    if (child < 0) return false;
    if (child == 0) {
        setsid();
        if (fork() != 0) _exit(0);
        const int null = open("/dev/null", O_RDWR);
        for (int fd = 0; fd < 3; ++fd)
            if (null >= 0) dup2(null, fd);
        for (int fd = 3; fd < files; ++fd) close(fd);
        setpriority(PRIO_PROCESS, 0, nice_level);
        execv(program, argv.data());
        _exit(127);
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}
