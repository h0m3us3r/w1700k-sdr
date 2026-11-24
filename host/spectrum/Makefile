# SPDX-License-Identifier: MIT
# w1700k-spectrum: a C++17 compiler, zlib and FFTW3 (single precision). The page in web/ is
# compiled into the binary; the stream comes from w1700k-stream (../stream). Tune for the build host
# with CXXFLAGS="-O3 -march=native".
CXX ?= c++
CXXFLAGS ?= -O3
WARNINGS ?= -Wall -Wextra -Werror
PREFIX ?= /usr/local
override CXXFLAGS += -std=c++17 -pthread -I../stream/include $(WARNINGS)
LDLIBS += -pthread -lz -lfftw3f -lrt

SOURCES := $(wildcard src/*.cpp)
OBJECTS := $(patsubst src/%.cpp,build/%.o,$(SOURCES)) build/web_assets.o build/placement.o build/apart.o

.PHONY: all clean install
all: build/w1700k-spectrum

build/w1700k-spectrum: $(OBJECTS)
	$(CXX) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/%.o: src/%.cpp $(wildcard src/*.h) ../stream/include/w1700k_stream.h | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

# Where the threads run beside the network stack: the stream service's own code.
build/placement.o: ../stream/src/placement.cpp ../stream/src/placement.h | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

# A process of the program's own, for measuring a plan: the stream service's code too.
build/apart.o: ../stream/src/apart.cpp ../stream/src/apart.h | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build/%.o: src/%.cpp ../stream/src/placement.h ../stream/src/apart.h

build/web_assets.cpp: embed.sh $(wildcard web/*) | build
	sh embed.sh web > $@

build/web_assets.o: build/web_assets.cpp src/web.h
	$(CXX) $(CXXFLAGS) -Isrc -c $< -o $@

build:
	mkdir -p $@

install: build/w1700k-spectrum
	install -D -m 755 $< $(DESTDIR)$(PREFIX)/bin/w1700k-spectrum

clean:
	rm -rf build
