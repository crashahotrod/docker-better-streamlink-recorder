#!/bin/bash
# Queue scheduling/state tests use handlers with observable filesystem results.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../pipeline.sh
source "$ROOT/pipeline.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=queue SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
pipeline_init
TEST_NOW=10000
date() {
    if [[ "${1:-}" == +%s ]]; then printf '%s\n' "$TEST_NOW"; else command date "$@"; fi
}
bad=$(mktemp -d "$WORK_DIR/publish/recording.XXXXXXXX")
good=$(mktemp -d "$WORK_DIR/publish/recording.XXXXXXXX")
touch "$bad/fail"
handler() {
    printf '%s\n' "${1##*/}" >> "$TEST_ROOT/calls"
    [[ ! -f "$1/fail" ]] || return 1
    advance_job "$1" encode
}
run_queue_once publish handler
[[ -d "$bad" && ! -d "$good" && -d "$WORK_DIR/encode/${good##*/}" ]]
[[ $(cat "$bad/publish.attempts") -eq 1 && $(cat "$bad/publish.retry-at") -eq 10030 ]]
[[ $(wc -l < "$TEST_ROOT/calls") -eq 2 ]]
run_queue_once publish handler
[[ $(wc -l < "$TEST_ROOT/calls") -eq 2 ]]
for delay in 60 120 240 480 900 900; do
    read -r TEST_NOW < "$bad/publish.retry-at"
    run_queue_once publish handler
    [[ $(cat "$bad/publish.retry-at") -eq $((TEST_NOW + delay)) ]]
done
[[ $(cat "$bad/publish.attempts") -eq 6 ]]
printf 'not-a-number\n' > "$bad/publish.retry-at"
printf 'not-a-number\n' > "$bad/publish.attempts"
run_queue_once publish handler
[[ $(cat "$bad/publish.attempts") -eq 1 && $(cat "$bad/publish.retry-at") -eq $((TEST_NOW + 30)) ]]
mv -- "$bad" "$TEST_ROOT/failed-job"

# WORKER_ONCE performs one scan, including transfers, without a polling sleep.
once=$(mktemp -d "$WORK_DIR/normalize/recording.XXXXXXXX")
WORKER_ONCE=true run_worker normalize handler
[[ ! -d "$once" && -d "$WORK_DIR/encode/${once##*/}" ]]

# Recovery advances only explicitly completed active jobs.
UPLOAD=false ENCODE=false REMUX=false new_recording_job partial queue 1
partial="$RECORDING_JOB"
touch "$partial/capture.bin"
if complete_recording "$partial"; then echo 'Empty capture completed' >&2; exit 1; fi
[[ ! -f "$partial/completed" ]]
new_recording_job completed queue 2
completed="$RECORDING_JOB"
printf 'captured bytes' > "$completed/capture.bin"
write_state "$completed/completed" true
recover_completed_recordings
[[ -d "$partial" && ! -d "$completed" && -s "$WORK_DIR/normalize/${completed##*/}/capture.bin" ]]

# A failed completion handoff retains its completion marker for restart recovery.
new_recording_job handoff queue 4
handoff="$RECORDING_JOB"
printf 'captured bytes' > "$handoff/capture.bin"
mkdir -- "$WORK_DIR/normalize/${handoff##*/}"
if complete_recording "$handoff"; then echo 'Completion collision accepted' >&2; exit 1; fi
[[ -f "$handoff/completed" && -s "$handoff/capture.bin" ]]
rmdir -- "$WORK_DIR/normalize/${handoff##*/}"
recover_completed_recordings
[[ ! -d "$handoff" && -s "$WORK_DIR/normalize/${handoff##*/}/capture.bin" ]]

# A competing monitor exits before any API request or recording can begin.
mkdir -- "$TEST_ROOT/bin"
cat > "$TEST_ROOT/bin/curl" <<'SH'
#!/bin/bash
touch "$TEST_NO_NETWORK_MARKER"
exit 1
SH
chmod +x -- "$TEST_ROOT/bin/curl"
(
    exec 9> "$WORK_DIR/download.lock"
    flock -n 9
    if PATH="$TEST_ROOT/bin:$PATH" CLIENT_ID=example CLIENT_SECRET=example \
        TEST_NO_NETWORK_MARKER="$TEST_ROOT/network-attempt" \
        timeout 3 bash "$ROOT/download.sh"; then
        echo 'Duplicate monitor started' >&2; exit 1
    else
        [[ $? -eq 1 ]]
    fi
)
[[ ! -e "$TEST_ROOT/network-attempt" ]]

# Job flags and original metadata survive changed environment values.
title=$(python3 -c 'print("caf\u00e9/\n" * 150, end="")')
UPLOAD=true ENCODE=true REMUX=true new_recording_job "$title" queue 3
job="$RECORDING_JOB"
UPLOAD=false ENCODE=false REMUX=false route_recording "$job"
job="$WORK_DIR/upload/${job##*/}"
[[ -d "$job" && $(cat "$job/remux-required") == true && $(cat "$job/encode-required") == true ]]
actual_title=''
read_field "$job/title" actual_title
[[ "$actual_title" == "$title" ]]
read_field "$job/stem" stem
[[ "$stem" != */* && "$stem" == *"${job##*/}" ]]
python3 - "$stem" <<'PY'
import sys
assert len((sys.argv[1] + ' - part-0001.mp4').encode()) <= 255
PY

# Recovery and cleanup must reject unrelated paths and unfinished captures.
outside="$TEST_ROOT/outside/recording.external"
mkdir -p -- "$outside"
printf 'keep' > "$outside/capture.bin"
touch "$outside/completed"
if bash "$ROOT/recover.sh" "$outside"; then echo 'External recovery job accepted' >&2; exit 1; fi
if bash "$ROOT/recover.sh" "$partial"; then echo 'Unfinished recovery job accepted' >&2; exit 1; fi
if remove_completed_job "$outside"; then echo 'External cleanup accepted' >&2; exit 1; fi
[[ $(cat "$outside/capture.bin") == keep && -d "$partial" ]]
printf 'PASS: retry deadlines/cap, queue progress, persisted flags, completion and recovery boundaries\n'
