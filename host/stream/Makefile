# SPDX-License-Identifier: MIT
# w1700k-stream: a C++17 compiler and FFTW3 (single precision). Tune for the build host with
# CXXFLAGS="-O3 -march=native".
CXX ?= c++
CXXFLAGS ?= -O3
WARNINGS ?= -Wall -Wextra -Werror
PREFIX ?= /usr/local
override CXXFLAGS += -std=c++17 -pthread -Iinclude -I../../include $(WARNINGS)
LDLIBS += -pthread -lfftw3f -lrt

SOURCES := $(wildcard src/*.cpp)
OBJECTS := $(patsubst src/%.cpp,build/%.o,$(SOURCES))

.PHONY: all clean install
all: build/w1700k-stream

build/w1700k-stream: $(OBJECTS)
	$(CXX) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/%.o: src/%.cpp $(wildcard src/*.h) include/w1700k_stream.h ../../include/hw/mt7996/iq_reduce.h | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build:
	mkdir -p $@

install: build/w1700k-stream
	install -D -m 755 $< $(DESTDIR)$(PREFIX)/bin/w1700k-stream
	install -D -m 644 include/w1700k_stream.h $(DESTDIR)$(PREFIX)/include/w1700k_stream.h

clean:
	rm -rf build
