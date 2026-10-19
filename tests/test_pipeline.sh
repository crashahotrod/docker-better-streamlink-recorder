#!/bin/bash
# Negative cases deliberately log failures; assertions verify retained inputs.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../encode.sh
source "$ROOT/encode.sh"
# shellcheck source=../upload.sh
source "$ROOT/upload.sh"
TEST_ROOT=$(mktemp -d)
cleanup() {
    local status=$?
    if (( status != 0 )) && [[ -n "${TEST_ARTIFACT_DIR:-}" ]]; then
        mkdir -p -- "$TEST_ARTIFACT_DIR/pipeline"
        cp -a -- "$TEST_ROOT/." "$TEST_ARTIFACT_DIR/pipeline/"
    fi
    find "$TEST_ROOT" -depth -delete
}
trap cleanup EXIT
export MODE=twitch CHANNEL=test SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
export UPLOAD_BOT_NAME=test
pipeline_init
python3 "$ROOT/tests/generate_media_fixtures.py" --intro-seconds 1 --main-seconds 1 --output "$TEST_ROOT/fixtures"
ffmpeg() { [[ "${FAIL_STAGE:-}" != encode ]] && command ffmpeg "$@"; }
exiftool() { [[ "${FAIL_STAGE:-}" != metadata ]] && command exiftool "$@"; }
ln() {
    if [[ "${FAIL_STAGE:-}" == second-part && "${!#}" == *part-0002.mp4 ]]; then return 1; fi
    command ln "$@"
}
new_job() {
    local fixture="${1:-control-single-init.mp4}" stage="${2:-encode}"
    REMUX=true UPLOAD=false ENCODE=true new_recording_job $'title " \\ cafe\nnew line' test 123
    cp -- "$TEST_ROOT/fixtures/$fixture" "$RECORDING_JOB/capture.bin"
    complete_recording "$RECORDING_JOB"
    JOB="$WORK_DIR/normalize/${RECORDING_JOB##*/}"
    ensure_normalized_job "$JOB"
    advance_job "$JOB" "$stage"
    JOB="$WORK_DIR/$stage/${JOB##*/}"
}
for FAIL_STAGE in encode metadata; do
    new_job
    if encode_job "$JOB"; then echo "Unexpected success: $FAIL_STAGE" >&2; exit 1; fi
    [[ -s "$JOB/capture.bin" ]]
    mv -- "$JOB" "$TEST_ROOT/failed-$FAIL_STAGE"
done
unset FAIL_STAGE
new_job
encode_job "$JOB"
[[ ! -d "$JOB" ]]
# Existing unrelated destinations are never replaced.
new_job
final=$(part_destination "$JOB" 0001 mp4)
printf 'other recording' > "$final"
if encode_job "$JOB"; then echo 'Collision unexpectedly succeeded' >&2; exit 1; fi
[[ -s "$JOB/capture.bin" && $(cat "$final") == 'other recording' ]]
mv -- "$JOB" "$TEST_ROOT/collision"
# Partial publication retains capture and resumes from the first part's receipt.
new_job duplicate-changed-codec.mp4 publish
FAIL_STAGE=second-part
if publish_recording "$JOB"; then echo 'Partial publication accepted' >&2; exit 1; fi
[[ -s "$JOB/capture.bin" ]]
first=$(part_destination "$JOB" 0001 mp4)
before=$(file_hash "$first")
unset FAIL_STAGE
publish_recording "$JOB"
[[ ! -d "$JOB" && $(file_hash "$first") == "$before" ]]
invoke_uploader() {
    local filename=''
    while (($#)); do
        if [[ "$1" == -filename ]]; then filename="$2"; fi
        shift
    done
    printf '%s\n' "${filename##*/}" >> "$TEST_ROOT/uploads"
    [[ "${FAIL_UPLOAD:-false}" != true || "$filename" != */0002.mp4 ]]
}
# Each upload has its own receipt and retry state survives worker restarts.
new_job duplicate-changed-codec.mp4 upload
FAIL_UPLOAD=true
run_queue_once upload upload_job
[[ -f "$JOB/parts/0001.uploaded" && ! -f "$JOB/parts/0002.uploaded" && -s "$JOB/capture.bin" ]]
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 2 && -f "$JOB/upload.retry-at" ]]
FAIL_UPLOAD=false
run_queue_once upload upload_job
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 2 ]]
printf '0\n' > "$JOB/upload.retry-at"
run_queue_once upload upload_job
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 3 ]]
[[ ! -d "$JOB" && -f "$WORK_DIR/encode/${JOB##*/}/parts/0002.uploaded" ]]
encode_job "$WORK_DIR/encode/${JOB##*/}"
# A handoff collision after upload doesn't cause a duplicate remote upload.
new_job control-single-init.mp4 upload
mkdir -- "$WORK_DIR/encode/${JOB##*/}"
if upload_job "$JOB"; then echo 'Handoff collision accepted' >&2; exit 1; fi
[[ -f "$JOB/parts/0001.uploaded" ]]
calls=$(wc -l < "$TEST_ROOT/uploads")
rmdir -- "$WORK_DIR/encode/${JOB##*/}"
upload_job "$JOB"
[[ $(wc -l < "$TEST_ROOT/uploads") -eq "$calls" ]]
# Locks exclude competing workers and a completed stage isn't processed twice.
new_job control-single-init.mp4 upload
calls=$(wc -l < "$TEST_ROOT/uploads")
(
    exec 8> "$WORK_DIR/upload.lock"
    flock -n 8
    run_queue_once upload upload_job
)
[[ -d "$JOB" && $(wc -l < "$TEST_ROOT/uploads") -eq "$calls" ]]
run_queue_once upload upload_job
run_queue_once upload upload_job
[[ ! -d "$JOB" && $(wc -l < "$TEST_ROOT/uploads") -eq $((calls + 1)) ]]
# Crash recovery can finish from committed receipts without re-encoding.
new_job
input=$(part_file "$JOB" 0001)
publish_part "$JOB" 0001 "$input"
FAIL_STAGE=encode encode_job "$JOB"
[[ ! -d "$JOB" ]]
# Legacy success receipts require reconciliation; partial writers stay active.
REMUX=false UPLOAD=false ENCODE=false new_recording_job legacy test 2
cp -- "$TEST_ROOT/fixtures/control-single-init.mp4" "$RECORDING_JOB/media.ts"
touch "$RECORDING_JOB/uploaded"
complete_recording "$RECORDING_JOB"
JOB="$WORK_DIR/normalize/${RECORDING_JOB##*/}"
run_queue_once normalize normalize_job
[[ -s "$WORK_DIR/quarantine/${JOB##*/}/media.ts" ]]
new_recording_job partial test 3
printf 'partial' > "$RECORDING_JOB/capture.bin"
recover_completed_recordings
[[ -d "$RECORDING_JOB" ]]
# Capture failures retain bytes in active, without a completion marker.
# shellcheck source=../download.sh
source "$ROOT/download.sh"
streamlink() {
    while (($#)); do
        if [[ "$1" == --output ]]; then printf 'partial capture' > "$2"; return 1; fi
        shift
    done
    return 1
}
if record_stream interrupted test 4; then echo 'Interrupted capture accepted' >&2; exit 1; fi
[[ -s "$RECORDING_JOB/capture.bin" && ! -f "$RECORDING_JOB/completed" ]]
# Offline recovery writes candidates beside the input and refuses an old manifest.
REMUX=true UPLOAD=false ENCODE=false new_recording_job recovery test 5
cp -- "$TEST_ROOT/fixtures/control-single-init.mp4" "$RECORDING_JOB/capture.bin"
original=$(file_hash "$RECORDING_JOB/capture.bin")
complete_recording "$RECORDING_JOB"
JOB="$WORK_DIR/normalize/${RECORDING_JOB##*/}"
advance_job "$JOB" quarantine
JOB="$WORK_DIR/quarantine/${JOB##*/}"
bash "$ROOT/recover.sh" "$JOB"
JOB="$WORK_DIR/normalize/${JOB##*/}"
[[ -f "$JOB/normalized.json" && $(file_hash "$JOB/capture.bin") == "$original" ]]
if bash "$ROOT/recover.sh" "$JOB"; then echo 'Existing manifest overwritten' >&2; exit 1; fi
[[ $(file_hash "$JOB/capture.bin") == "$original" ]]
printf 'PASS: retained inputs, per-part receipts, retries, locks and crash recovery\n'
