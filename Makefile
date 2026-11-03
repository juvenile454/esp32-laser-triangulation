CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Werror
SANITIZE_FLAGS := -fsanitize=address,undefined -fno-omit-frame-pointer -O1 -g
INCLUDES := -Ilib/laser_core/src
CORE := lib/laser_core/src/laser_core.cpp
BUILD := build/host

.PHONY: all test sanitize clean

all: $(BUILD)/laser_cli $(BUILD)/test_laser_core

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/laser_cli: host/laser_cli.cpp $(CORE) lib/laser_core/src/laser_core.h | $(BUILD)
	$(CXX) $(CXXFLAGS) $(INCLUDES) host/laser_cli.cpp $(CORE) -o $@

$(BUILD)/test_laser_core: tests/test_laser_core.cpp $(CORE) lib/laser_core/src/laser_core.h | $(BUILD)
	$(CXX) $(CXXFLAGS) $(INCLUDES) tests/test_laser_core.cpp $(CORE) -o $@

test: all
	$(BUILD)/test_laser_core

sanitize: | $(BUILD)
	$(CXX) $(CXXFLAGS) $(SANITIZE_FLAGS) $(INCLUDES) tests/test_laser_core.cpp $(CORE) -o $(BUILD)/test_laser_core_sanitize
	ASAN_OPTIONS=detect_leaks=1 $(BUILD)/test_laser_core_sanitize

clean:
	rm -rf $(BUILD)
