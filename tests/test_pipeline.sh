#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../encode.sh
source "$ROOT/encode.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=test SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
pipeline_init

ffmpeg() {
    [[ "${FAIL_STAGE:-}" != encode ]] || return 1
    local input='' output="${!#}"
    while (($#)); do
        if [[ "$1" == -i ]]; then input="$2"; fi
        shift
    done
    if [[ "$input" == pipe:0 ]]; then cat > "$output"; else cp -- "$input" "$output"; fi
}
ffprobe() { [[ "${FAIL_STAGE:-}" != validation ]] && printf '{"streams":[{"codec_type":"video"}]}\n'; }
exiftool() { [[ "${FAIL_STAGE:-}" != metadata ]]; }

new_job() {
    JOB=$(mktemp -d "$WORK_DIR/encode/recording.XXXXXXXX")
    printf 'fixture' > "$JOB/media.${1:-ts}"
    printf '%s\0' "title with spaces \\ and"$'\n'"a newline" > "$JOB/title"
    printf '%s\0' "test - s202610e1801 - ${JOB##*/}" > "$JOB/stem"
    printf '202610\n' > "$JOB/season"
}

for FAIL_STAGE in encode validation metadata; do
    new_job
    if encode_job "$JOB"; then echo "Unexpected success: $FAIL_STAGE" >&2; exit 1; fi
    [[ -f "$JOB/media.ts" ]]
done
unset FAIL_STAGE
new_job mp4
encode_job "$JOB"
[[ ! -d "$JOB" ]]
[[ $(find "$STORAGE_DIR" -name '*.mp4' | wc -l) -eq 1 ]]

# Existing unrelated destinations are never replaced, and retain the source.
new_job
read_field "$JOB/stem" stem
printf 'other recording' > "$STORAGE_DIR/$CHANNEL/Season 202610/$stem.mp4"
if encode_job "$JOB"; then echo 'Collision unexpectedly succeeded' >&2; exit 1; fi
[[ -f "$JOB/media.ts" ]]
[[ $(cat "$STORAGE_DIR/$CHANNEL/Season 202610/$stem.mp4") == 'other recording' ]]
printf 'PASS: encoding failures preserve sources; MP4 input; collision protection\n'

# shellcheck source=../download.sh
source "$ROOT/download.sh"
streamlink() {
    local output=''
    while (($#)); do
        if [[ "$1" == --output ]]; then output="$2"; fi
        shift
    done
    if [[ -n "$output" ]]; then printf 'fixture' > "$output"; else printf 'fixture'; fi
}
for MODE in twitch kick; do
    pipeline_init
    for REMUX in false true; do
        for UPLOAD in false true; do
            for ENCODE in false true; do
                record_stream $'quotes " slash \\ Unicode é\nnew line /' test 123
                stage=publish
                if [[ "$ENCODE" == true ]]; then stage=encode; fi
                if [[ "$UPLOAD" == true ]]; then stage=upload; fi
                job="$WORK_DIR/$stage/${RECORDING_JOB##*/}"
                [[ -f "$job/completed" && ! -d "$RECORDING_JOB" ]]
                extension=ts
                if [[ "$REMUX" == true ]]; then extension=mp4; fi
                [[ -s "$job/media.$extension" ]]
                if [[ "$stage" == publish ]]; then publish_recording "$job"; fi
            done
        done
    done
done
# Completion recovery ignores uncertain active recordings.
new_recording_job partial test 123
partial="$RECORDING_JOB"
printf 'partial' > "$partial/media.ts"
recover_completed_recordings
[[ -d "$partial" ]]
printf 'PASS: both services and all flag routes; active recordings excluded\n'

# shellcheck source=../upload.sh
source "$ROOT/upload.sh"
CHANNEL=recovery
pipeline_init
export UPLOAD_BOT_NAME=test
invoke_uploader() {
    printf 'called\n' >> "$TEST_ROOT/uploads"
    [[ "${FAIL_UPLOAD:-false}" != true ]]
}
UPLOAD=true ENCODE=true REMUX=false
record_stream recovery test 123
job="$WORK_DIR/upload/${RECORDING_JOB##*/}"
FAIL_UPLOAD=true
run_queue_once upload upload_job
[[ -s "$job/media.ts" && -f "$job/upload.retry-at" && ! -f "$job/uploaded" ]]
FAIL_UPLOAD=false
run_queue_once upload upload_job
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 1 ]]
printf '0\n' > "$job/upload.retry-at"
run_queue_once upload upload_job
[[ ! -d "$job" && -f "$WORK_DIR/encode/${job##*/}/uploaded" ]]
run_queue_once encode encode_job
[[ ! -d "$WORK_DIR/encode/${job##*/}" ]]

# Success marker survives failed local handoff. A second process can resume it
# without uploading again. Create a target collision to fail only the move.
record_stream recovery test 456
job="$WORK_DIR/upload/${RECORDING_JOB##*/}"
mkdir -- "$WORK_DIR/encode/${job##*/}"
if upload_job "$job"; then echo 'Handoff collision succeeded' >&2; exit 1; fi
[[ -f "$job/uploaded" ]]
calls=$(wc -l < "$TEST_ROOT/uploads")
rmdir -- "$WORK_DIR/encode/${job##*/}"
upload_job "$job"
[[ $(wc -l < "$TEST_ROOT/uploads") -eq "$calls" ]]

# Stage locks exclude a competing worker and are released after process exit.
record_stream locked test 789
job="$WORK_DIR/upload/${RECORDING_JOB##*/}"
calls=$(wc -l < "$TEST_ROOT/uploads")
(
    exec 8> "$WORK_DIR/upload.lock"
    flock -n 8
    run_queue_once upload upload_job
)
[[ -d "$job" && $(wc -l < "$TEST_ROOT/uploads") -eq "$calls" ]]
run_queue_once upload upload_job
[[ ! -d "$job" && $(wc -l < "$TEST_ROOT/uploads") -eq $((calls + 1)) ]]
run_queue_once upload upload_job
[[ $(wc -l < "$TEST_ROOT/uploads") -eq $((calls + 1)) ]]

# Recover the crash window after publication, even if source cleanup started.
new_job
publish_job "$JOB" "$JOB/media.ts" mp4
rm -- "$JOB/media.ts"
encode_job "$JOB"
[[ ! -d "$JOB" ]]
printf 'PASS: retry persistence, upload handoff recovery, locks, publication recovery\n'
