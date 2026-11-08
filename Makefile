# Host build, checks and firmware cross-build.
#
#   make check     test sanitize std11 lint typecheck py-test, in that order
#   make firmware  PlatformIO cross-build of the ESP32-S3 firmware
#
# CXX, CXXFLAGS, PYTHON and PIO can be overridden from the environment or the
# command line, e.g. `make CXX=clang++ test`.

# GNU make predefines CXX (origin "default"), so `CXX ?= ...` would never
# apply; only replace the built-in default, never a user-provided compiler.
ifeq ($(origin CXX),default)
CXX := g++
endif
WARNINGS := -Wall -Wextra -Wpedantic -Wconversion -Werror
CXXFLAGS ?= -std=c++17 -O2 $(WARNINGS)
SANITIZE_FLAGS := -fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer -O1 -g
STD11_FLAGS := -std=c++11 -Wall -Wextra -Wpedantic -Wconversion -Werror -fsyntax-only
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PIO ?= $(if $(wildcard .venv/bin/pio),.venv/bin/pio,pio)

INCLUDES := -Ilib/laser_core/src
CORE := lib/laser_core/src/laser_core.cpp
CORE_HEADERS := lib/laser_core/src/laser_core.h lib/laser_core/src/inference_contract.h
COMPILE_CHECK := src/laser_contract_compile_check.cpp
BUILD := build/host

.PHONY: all test sanitize std11 lint typecheck py-test check firmware clean

# Goals such as `make -j clean test sanitize` must run one after another:
# clean would otherwise race the builds that follow it.
.NOTPARALLEL:

all: $(BUILD)/laser_cli $(BUILD)/test_laser_core

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/laser_cli: host/laser_cli.cpp $(CORE) $(CORE_HEADERS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(INCLUDES) host/laser_cli.cpp $(CORE) -o $@

$(BUILD)/test_laser_core: tests/test_laser_core.cpp $(CORE) $(CORE_HEADERS) | $(BUILD)
	$(CXX) $(CXXFLAGS) $(INCLUDES) tests/test_laser_core.cpp $(CORE) -o $@

test: all
	$(BUILD)/test_laser_core

sanitize: | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SANITIZE_FLAGS) $(INCLUDES) tests/test_laser_core.cpp $(CORE) -o $(BUILD)/test_laser_core_sanitize
	ASAN_OPTIONS=detect_leaks=1 $(BUILD)/test_laser_core_sanitize

# The firmware toolchain may compile the portable core as gnu++11.
std11:
	$(CXX) $(STD11_FLAGS) $(INCLUDES) $(CORE)
	$(CXX) $(STD11_FLAGS) $(INCLUDES) $(COMPILE_CHECK)

lint:
	$(PYTHON) -m ruff check tools tests

typecheck:
	$(PYTHON) -m mypy

# The C++/Python parity tests need build/host/laser_cli.
py-test: all
	$(PYTHON) -m unittest discover -s tests -v

check: test sanitize std11 lint typecheck py-test

firmware:
	$(PIO) run -j 1

clean:
	rm -rf $(BUILD)
