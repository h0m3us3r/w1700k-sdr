# SPDX-License-Identifier: MIT
# The SoapySDR module (driver=w1700k): a C++17 compiler and SoapySDR's headers (Debian/Ubuntu:
# libsoapysdr-dev). It reads w1700k-stream's shared memory (../stream/include) and shares the
# spectrum server's JSON reader (../spectrum/src/json.cpp).
CXX ?= c++
CXXFLAGS ?= -O3
WARNINGS ?= -Wall -Wextra -Werror
PREFIX ?= /usr/local
# Where SoapySDR looks for modules under PREFIX: modules<ABI>, the ABI from its headers.
SOAPY_ABI := $(shell sed -n 's/^\#define SOAPY_SDR_ABI_VERSION "\(.*\)"/\1/p' \
	$$(pkg-config --variable=includedir SoapySDR 2>/dev/null || echo /usr/include)/SoapySDR/Version.h 2>/dev/null)
MODULE_DIR ?= $(PREFIX)/lib/SoapySDR/modules$(SOAPY_ABI)
override CXXFLAGS += -std=c++17 -fPIC -fvisibility=hidden -pthread -Isrc -I../stream/include -I../spectrum/src \
	$(shell pkg-config --cflags SoapySDR 2>/dev/null) $(WARNINGS)
LDLIBS += $(shell pkg-config --libs SoapySDR 2>/dev/null || echo -lSoapySDR) -pthread -lrt

OBJECTS := build/w1700k_soapy.o build/control.o build/ring.o build/json.o

.PHONY: all clean install
all: build/libw1700kSupport.so

build/libw1700kSupport.so: $(OBJECTS)
	$(CXX) -shared $(LDFLAGS) $^ $(LDLIBS) -o $@

build/%.o: src/%.cpp $(wildcard src/*.h) ../stream/include/w1700k_stream.h ../spectrum/src/json.h | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build/json.o: ../spectrum/src/json.cpp ../spectrum/src/json.h | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build:
	mkdir -p $@

install: build/libw1700kSupport.so
	install -D -m 755 $< $(DESTDIR)$(MODULE_DIR)/libw1700kSupport.so

clean:
	rm -rf build
