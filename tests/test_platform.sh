#!/bin/bash
# Small offline end-to-end smoke check for each advertised image architecture.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../pipeline.sh
source "$ROOT/pipeline.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
export MODE=twitch CHANNEL=platform REMUX=false UPLOAD=false ENCODE=false
python3 -c 'import streamlink.webbrowser.chromium'
streamlink --version
rclone version
/etc/youtubeuploader -version
python3 "$ROOT/tests/generate_media_fixtures.py" --intro-seconds 1 --main-seconds 1 --output "$TEST_ROOT/fixtures"
pipeline_init
new_recording_job platform platform 1
cp -- "$TEST_ROOT/fixtures/duplicate-changed-codec.mp4" "$RECORDING_JOB/capture.bin"
complete_recording "$RECORDING_JOB"
identifier="${RECORDING_JOB##*/}"
normalize_job "$WORK_DIR/normalize/$identifier"
[[ -s "$WORK_DIR/publish/$identifier/capture.bin" ]]
publish_recording "$WORK_DIR/publish/$identifier"
[[ ! -d "$WORK_DIR/publish/$identifier" ]]
files=()
mapfile -d '' -t files < <(find "$STORAGE_DIR" -type f -name '*.mp4' -print0)
[[ "${#files[@]}" -eq 2 ]]
for file in "${files[@]}"; do
    count=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$file")
    [[ "$count" -eq 10 ]]
done
printf 'PASS: platform dependencies, initialization repair and application-user publication\n'
