// SPDX-License-Identifier: MIT
// A process of this program's own, started apart: for work that takes long, need not be waited
// for and is worth finishing if the program that asked for it ends (FFTW's measured plans:
// flatten.h, and the spectrum server's).
#pragma once

#include <string>
#include <vector>

namespace apart {

// Starts this program's own file again with `arguments`, in a session of its own (a Ctrl-C meant
// for this process is not for it), as nobody's child (it is not waited for and goes on if this
// process ends), at the nice level given, with no terminal and with none of this process's open
// files: a socket of the stream's port in a process that reads nothing would be sent a share of
// the stream by the kernel, and a listening one would keep its port. False if it could not be
// started; how it ends is not known here.
bool run(const std::vector<std::string> &arguments, int nice_level = 0);

}  // namespace apart
