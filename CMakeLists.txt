# Top-level dev orchestration CMake.
#
# TEMPORARY: this file ties the three sub-projects together while they share the
# Omni-84 DecentSampler library repo. When each sub-project moves to its own repo
# (see PLAN.md), it gets its own top-level CMakeLists that fetches JUCE and pulls
# `dehli-musikk-sampler-engine` in via submodule/FetchContent. For now, this root does the
# JUCE fetch once and adds the sub-projects.

cmake_minimum_required(VERSION 3.22)

# Universal macOS binaries (Apple Silicon + Intel) — must be set BEFORE project().
# A single-arch Intel build runs under Rosetta on ARM Macs, which macOS is
# deprecating ("Support ending for Intel-based apps"). Applies to fresh build dirs
# only: an existing build dir keeps its cached architectures.
if(CMAKE_HOST_APPLE AND NOT CMAKE_OSX_ARCHITECTURES)
    set(CMAKE_OSX_ARCHITECTURES "arm64;x86_64" CACHE STRING "Build architectures for macOS")
endif()
if(CMAKE_HOST_APPLE AND NOT CMAKE_OSX_DEPLOYMENT_TARGET)
    set(CMAKE_OSX_DEPLOYMENT_TARGET "11.0" CACHE STRING "Minimum macOS version")
endif()

project(Omni84Suite VERSION 0.1.0 LANGUAGES C CXX)

# Single-config generators default to NO build type = unoptimized binaries that the
# packaging scripts then can't find (they expect .../_artefacts/Release). Default to
# Release; pass -DCMAKE_BUILD_TYPE=Debug explicitly when debugging.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Place all build artifacts under build/ (git-ignored).
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Enable CTest at the top level so engine unit tests register (see
# dehli-musikk-sampler-engine/CMakeLists.txt). Run with: ctest --test-dir build
enable_testing()

# ---------------------------------------------------------------------------
# JUCE — fetched once here and shared by every sub-project.
# Bump JUCE_GIT_TAG as needed; any JUCE 8.0.x tag should work.
# ---------------------------------------------------------------------------
include(FetchContent)

set(JUCE_GIT_TAG "8.0.4" CACHE STRING "JUCE git tag to build against")

FetchContent_Declare(
    JUCE
    GIT_REPOSITORY https://github.com/juce-framework/JUCE.git
    GIT_TAG        ${JUCE_GIT_TAG}
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(JUCE)

# Shared plugin declaration: dmse_add_plugin(Target PRODUCT_NAME ... PLUGIN_CODE ...
# VERSION ...) — the whole per-product build (juce_add_plugin constants, shared entry
# point, asset embed, pack install, custom standalone, packaging metadata) lives in the
# ENGINE repo so it's versioned once, not copy-pasted 13×.
include(dehli-musikk-sampler-engine/cmake/DmsePlugin.cmake)

# ---------------------------------------------------------------------------
# Sub-projects
# ---------------------------------------------------------------------------
add_subdirectory(dehli-musikk-sampler-engine)
add_subdirectory(omni-84-plugin)
add_subdirectory(maskintrommer-plugin)
add_subdirectory(midnight-wurli-plugin)
add_subdirectory(elektrisk-salmesykkel-plugin)
add_subdirectory(4-track-glockenspiel-plugin)
add_subdirectory(4-track-toy-piano-plugin)
add_subdirectory(4-track-music-box-plugin)
add_subdirectory(edb-orgel-plugin)
add_subdirectory(strykebrett-plugin)
add_subdirectory(voltage-controlled-cassette-organ-plugin)
add_subdirectory(lo-fi-tape-piano-plugin)
add_subdirectory(stylopoly-plugin)
add_subdirectory(subc-plugin)

# Minimal converter path lands in M2 (full CLI in M7).
add_subdirectory(ds-plugin-converter)

# The authoring app (its own repo, like the plugins) — optional, so a checkout
# without it still configures.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/dehli-musikk-sampler-plugin-editor/CMakeLists.txt")
    add_subdirectory(dehli-musikk-sampler-plugin-editor)
endif()
