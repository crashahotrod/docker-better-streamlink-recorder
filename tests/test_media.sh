#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../download.sh
source "$ROOT/download.sh"
# shellcheck source=../upload.sh
source "$ROOT/upload.sh"
# shellcheck source=../encode.sh
source "$ROOT/encode.sh"
TEST_ROOT=$(mktemp -d)
STORAGE_DIR=$(mktemp -d /dev/shm/recorder-tests.XXXXXXXX)
worker=''
cleanup() {
    local status=$?
    if [[ -n "$worker" ]]; then kill -TERM -- "-$worker" 2>/dev/null || true; wait "$worker" || true; fi
    if (( status != 0 )) && [[ -n "${TEST_ARTIFACT_DIR:-}" ]]; then
        mkdir -p -- "$TEST_ARTIFACT_DIR/media"
        cp -a -- "$TEST_ROOT/." "$TEST_ARTIFACT_DIR/media/"
    fi
    find "$TEST_ROOT" "$STORAGE_DIR" -depth -delete
}
trap cleanup EXIT
export TEST_ROOT STORAGE_DIR
export SCRATCH_ROOT="$TEST_ROOT/scratch space"$'\\\n' CONFIG_DIR="$TEST_ROOT/config"
export MODE=twitch CHANNEL=media UPLOAD_BOT_NAME='test' YOUTUBE_PRIVACY=private
export YOUTUBEUPLOADER_BIN="$TEST_ROOT/bin/youtubeuploader"
mkdir -p -- "$TEST_ROOT/bin" "$CONFIG_DIR"
export FIXTURE="$TEST_ROOT/fixture.ts"
ffmpeg -nostdin -v error -f lavfi -i testsrc=size=160x90:rate=10 \
    -f lavfi -i sine=frequency=800 -t 1 -c:v libx264 -pix_fmt yuv420p \
    -c:a aac -f mpegts "$FIXTURE"
cat > "$TEST_ROOT/bin/streamlink" <<'SH'
#!/bin/bash
set -euo pipefail
output=''
url="${@: -2:1}"
quality="${!#}"
while (($#)); do
    if [[ "$1" == --output ]]; then output="$2"; fi
    shift
done
if [[ "$MODE" == twitch ]]; then expected="twitch.tv/$CHANNEL"; else expected="kick.com/$CHANNEL"; fi
[[ "$url" == "$expected" && "$quality" == best ]]
if [[ -n "$output" ]]; then cp -- "$FIXTURE" "$output"; else cat -- "$FIXTURE"; fi
SH
cat > "$YOUTUBEUPLOADER_BIN" <<'SH'
#!/bin/bash
set -euo pipefail
printf 'called\n' >> "$TEST_ROOT/uploads"
args=("$@")
input=''
for ((i=0; i<${#args[@]}; i++)); do
    if [[ "${args[i]}" == -filename ]]; then input="${args[i+1]}"; fi
done
[[ -s "$input" ]]
read -r recorded_date < "${input%/parts/*}/recorded-date"
while (($#)); do
    case "$1" in
        -privacy) [[ "$2" == "${YOUTUBE_PRIVACY:-public}" ]] ;;
        -title) [[ "$2" == "$recorded_date - "* ]] ;;
        -secrets) [[ "$2" == "$CONFIG_DIR/youtubeuploader_client_secrets.json" ]] ;;
        -cache) [[ "$2" == "$CONFIG_DIR/youtubeuploader_request.token" ]] ;;
        -metaJSONout) printf '{"id":"test-video"}\n' > "$2" ;;
    esac
    shift
done
SH
chmod +x -- "$TEST_ROOT/bin/streamlink" "$YOUTUBEUPLOADER_BIN"
export PATH="$TEST_ROOT/bin:$PATH"
# Exercise API response parsing while keeping all external requests mocked.
refresh_token() { :; }
fetch_stream_info() {
    if [[ "$MODE" == twitch ]]; then
        jq -n --arg title "$TITLE" '{data:[{title:$title,user_name:"media",id:"123"}]}'
    else
        jq -n --arg title "$TITLE" '{data:[{stream_title:$title,slug:"media",stream:{is_live:true,start_time:"2026-10-18T00:00:00Z"}}]}'
    fi
}
TITLE=$'title "quotes" café \\ and\nnewline / end'
for MODE in twitch kick; do
    pipeline_init
    for REMUX in false true; do
        for UPLOAD in false true; do
            for ENCODE in false true; do
                monitor_once
                identifier="${RECORDING_JOB##*/}"
                run_queue_once normalize normalize_job
                run_queue_once upload upload_job
                run_queue_once encode encode_job
                run_queue_once publish publish_recording
                files=()
                mapfile -d '' -t files < <(find "$STORAGE_DIR/$CHANNEL" -type f -name "*$identifier.*" \
                    \( -name '*.mp4' -o -name '*.ts' \) -print0)
                [[ "${#files[@]}" -eq 1 ]]
                final="${files[0]}"
                ffmpeg -nostdin -v error -xerror -i "$final" -f null -
                format=$(ffprobe -v error -show_entries format=format_name -of default=nw=1:nk=1 "$final")
                if [[ "$REMUX" == true || "$ENCODE" == true ]]; then
                    [[ "$final" == *.mp4 && "$format" == *mp4* ]]
                    # Verify metadata survives special characters and newlines.
                    actual_title=$(exiftool -b -Title "$final")
                    [[ "$actual_title" == "$TITLE" ]]
                else
                    [[ "$final" == *.ts && "$format" == mpegts ]]
                fi
                [[ ! -d "$WORK_DIR/active/$identifier" && ! -d "$WORK_DIR/upload/$identifier" &&
                   ! -d "$WORK_DIR/encode/$identifier" && ! -d "$WORK_DIR/publish/$identifier" ]]
            done
        done
    done
done
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 8 ]]
[[ $(stat -c %d "$SCRATCH_ROOT") != "$(stat -c %d "$STORAGE_DIR")" ]]
printf 'PASS: real media and metadata, both APIs, all 16 routes, separate filesystems\n'

# Every supported visibility is passed to the uploader for each captured route.
UPLOAD=true ENCODE=false REMUX=false
for YOUTUBE_PRIVACY in public unlisted; do
    record_stream visibility media 8
    identifier="${RECORDING_JOB##*/}"
    run_queue_once normalize normalize_job
    upload_job "$WORK_DIR/upload/$identifier"
    publish_recording "$WORK_DIR/publish/$identifier"
    [[ ! -d "$WORK_DIR/publish/$identifier" ]]
done
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 10 ]]
# Default visibility and title date use persisted job metadata on a later retry.
unset YOUTUBE_PRIVACY
record_stream dated media 8
identifier="${RECORDING_JOB##*/}"
run_queue_once normalize normalize_job
printf '01/02/2000\n' > "$WORK_DIR/upload/$identifier/recorded-date"
upload_job "$WORK_DIR/upload/$identifier"
publish_recording "$WORK_DIR/publish/$identifier"
[[ $(wc -l < "$TEST_ROOT/uploads") -eq 11 && ! -d "$WORK_DIR/publish/$identifier" ]]
export YOUTUBE_PRIVACY=private

# Explicit encoding preserves variable presentation times and optional audio.
FIXTURE="$TEST_ROOT/variable.mp4"
ffmpeg -nostdin -v error -f lavfi -i testsrc2=size=160x90:rate=10:duration=2 \
    -vf 'select=not(eq(mod(n\,3)\,1))' -fps_mode vfr -c:v libx264 "$FIXTURE"
UPLOAD=false ENCODE=true REMUX=false
record_stream variable media 9
identifier="${RECORDING_JOB##*/}"
run_queue_once normalize normalize_job
encode_job "$WORK_DIR/encode/$identifier"
[[ ! -d "$WORK_DIR/encode/$identifier" && ! -d "$WORK_DIR/quarantine/$identifier" ]]
FIXTURE="$TEST_ROOT/fixture.ts"

UPLOAD=false ENCODE=true REMUX=false
record_stream invalid media 1
run_queue_once normalize normalize_job
job="$WORK_DIR/encode/${RECORDING_JOB##*/}"
printf 'invalid input' > "$job/capture.bin"
if encode_job "$job"; then echo 'Invalid media accepted' >&2; exit 1; fi
job="$WORK_DIR/quarantine/${job##*/}"
[[ -s "$job/capture.bin" ]]
invalid_job="$job"

# Destination permission errors retain the full source and can be retried later.
record_stream unwritable media 3
run_queue_once normalize normalize_job
job="$WORK_DIR/encode/${RECORDING_JOB##*/}"
season_dir="$STORAGE_DIR/$CHANNEL/Season $(date +%Y%m)"
chmod 500 -- "$season_dir"
if encode_job "$job"; then echo 'Unwritable destination accepted' >&2; exit 1; fi
[[ -s "$job/capture.bin" ]]
chmod 700 -- "$season_dir"
mv -- "$job" "$TEST_ROOT/unwritable-job"

# Simulate a process-group interruption while FFmpeg has written partial output.
record_stream interrupted media 2
run_queue_once normalize normalize_job
job="$WORK_DIR/encode/${RECORDING_JOB##*/}"
mkdir -- "$TEST_ROOT/blockbin"
cat > "$TEST_ROOT/blockbin/ffmpeg" <<'SH'
#!/bin/bash
printf 'partial output' > "${!#}"
touch "$TEST_ROOT/encoding-started"
sleep 60
SH
chmod +x -- "$TEST_ROOT/blockbin/ffmpeg"
# Remove the invalid job from the queue before the isolated interruption test.
mv -- "$invalid_job" "$TEST_ROOT/invalid-job"
PATH="$TEST_ROOT/blockbin:$PATH" WORKER_ONCE=true setsid bash "$ROOT/encode.sh" &
worker=$!
for ((i=0; i<100; i++)); do
    [[ ! -f "$TEST_ROOT/encoding-started" ]] || break
    sleep 0.1
done
[[ -f "$TEST_ROOT/encoding-started" ]]
kill -TERM -- "-$worker"
wait "$worker" || true
worker=''
[[ -s "$job/capture.bin" && -s "$job/parts/0001.encoded.mp4" ]]
# A fresh process obtains the released lock and replaces only the partial output.
WORKER_ONCE=true bash "$ROOT/encode.sh"
[[ ! -d "$job" ]]
printf 'PASS: invalid media retained; interrupted encode resumes in a fresh process\n'

# Interrupt the Python normalizer and all its FFmpeg children, then resume.
UPLOAD=false ENCODE=false REMUX=true
record_stream normalization media 10
job="$WORK_DIR/normalize/${RECORDING_JOB##*/}"
original=$(file_hash "$job/capture.bin")
mkdir -- "$TEST_ROOT/normalizer-bin"
cat > "$TEST_ROOT/normalizer-bin/ffmpeg" <<'SH'
#!/bin/bash
echo 'partial validation output'
touch "$TEST_ROOT/normalization-started"
sleep 60
SH
chmod +x -- "$TEST_ROOT/normalizer-bin/ffmpeg"
PATH="$TEST_ROOT/normalizer-bin:$PATH" WORKER_ONCE=true setsid bash "$ROOT/normalize.sh" &
worker=$!
for ((i=0; i<100; i++)); do
    [[ ! -f "$TEST_ROOT/normalization-started" ]] || break
    sleep 0.1
done
[[ -f "$TEST_ROOT/normalization-started" ]]
kill -TERM -- "-$worker"
wait "$worker" || true
worker=''
[[ $(file_hash "$job/capture.bin") == "$original" && ! -f "$job/normalized.json" ]]

# A subsequent ENOSPC from FFmpeg remains retryable, with the same recovery input.
cat > "$TEST_ROOT/normalizer-bin/ffmpeg" <<'SH'
#!/bin/bash
echo 'No space left on device' >&2
exit 1
SH
PATH="$TEST_ROOT/normalizer-bin:$PATH" WORKER_ONCE=true bash "$ROOT/normalize.sh"
[[ $(file_hash "$job/capture.bin") == "$original" && -f "$job/normalize.retry-at" ]]
jq -e '.status == "retry"' "$job/validation.json" > /dev/null
printf '0\n' > "$job/normalize.retry-at"
WORKER_ONCE=true bash "$ROOT/normalize.sh"
job="$WORK_DIR/publish/${job##*/}"
[[ $(file_hash "$job/capture.bin") == "$original" && -f "$job/normalized.json" ]]
publish_recording "$job"
[[ ! -d "$job" ]]
printf 'PASS: interrupted normalization and simulated disk-full resume with original bytes intact\n'
