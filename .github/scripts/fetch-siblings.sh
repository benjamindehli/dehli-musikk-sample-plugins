#!/usr/bin/env bash
# Clones the engine, the converter and the 13 plugin repos into the folders the
# root CMakeLists.txt adds, for the workflows that build the workspace.
#
# The repos are public under github.com/benjamindehli. Their names differ from
# the local folder names (no "-plugin" suffix, different casing), so each is
# cloned into the folder the root CMake expects. All 13 plugins must be present
# for the workspace to configure.
set -euo pipefail

# "<github-repo> <folder>", the folder matching the root add_subdirectory().
repos=(
  "dehli-musikk-sampler-engine dehli-musikk-sampler-engine"
  "ds-plugin-converter ds-plugin-converter"
  "Omni-84 omni-84-plugin"
  "MaskinTrommer maskintrommer-plugin"
  "MidnightWurli midnight-wurli-plugin"
  "ElektriskSalmesykkel elektrisk-salmesykkel-plugin"
  "4-track-glockenspiel 4-track-glockenspiel-plugin"
  "4-track-toy-piano 4-track-toy-piano-plugin"
  "4-track-music-box 4-track-music-box-plugin"
  "EDB-Orgel edb-orgel-plugin"
  "Strykebrett strykebrett-plugin"
  "VoltageControlledCassetteOrgan voltage-controlled-cassette-organ-plugin"
  "lo-fi-tape-piano lo-fi-tape-piano-plugin"
  "StyloPoly stylopoly-plugin"
  "SubC subc-plugin"
)

for pair in "${repos[@]}"; do
  set -- $pair
  echo "== cloning $1 -> $2 =="
  git clone --depth 1 "https://github.com/benjamindehli/$1.git" "$2"
done
