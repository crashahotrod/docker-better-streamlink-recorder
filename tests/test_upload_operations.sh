#!/bin/bash
# Upload bookkeeping and shared quota tests isolate media from remote operations.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../upload.sh
source "$ROOT/upload.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=one SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
export CONFIG_DIR="$TEST_ROOT/config" UPLOAD_BOT_NAME=test DISCORD_WEBHOOK_URL=https://example.invalid/public
mkdir -p "$CONFIG_DIR"
printf '{"installed":{"client_id":"shared"}}' > "$CONFIG_DIR/youtubeuploader_client_secrets.json"
pipeline_init
TEST_NOW=$(date +%s)
date() {
    if [[ "${1:-}" == +%s ]]; then printf '%s\n' "$TEST_NOW"; else command date "$@"; fi
}
ensure_normalized_job() { return 0; }
part_ids() { printf '0001\n'; }
part_file() { printf '%s/parts/0001.mp4\n' "$1"; }
new_upload() {
    new_recording_job 'Test upload' test 1
    JOB="$RECORDING_JOB"
    mkdir "$JOB/parts"
    printf fixture > "$JOB/parts/0001.mp4"
    printf '{"parts":[{"id":"0001"}]}' > "$JOB/normalized.json"
    printf 'false\n' > "$JOB/encode-required"
    advance_job "$JOB" upload
    JOB="$WORK_DIR/upload/${JOB##*/}"
}
invoke_uploader() {
    printf attempt >> "$TEST_ROOT/attempts"
    local previous='' arg
    for arg in "$@"; do
        if [[ "$previous" == -title ]]; then printf '%s' "$arg" > "$TEST_ROOT/last_title"; fi
        if [[ "$previous" == -description ]]; then printf '%s' "$arg" > "$TEST_ROOT/last_description"; fi
        if [[ "$previous" == -tags ]]; then printf '%s' "$arg" > "$TEST_ROOT/last_tags"; fi
        if [[ "$previous" == -thumbnail ]]; then printf '%s' "$arg" > "$TEST_ROOT/last_thumbnail"; fi
        previous="$arg"
    done
    if [[ "${FAIL_QUOTA:-false}" == true ]]; then printf '%s' "${QUOTA_ERROR:-quotaExceeded}"; return 1; fi
    return 0
}
notify_discord() {
    [[ -f "$CURRENT_JOB/parts/0001.uploaded" ]] || { echo 'Notification before receipt' >&2; exit 99; }
    return 1
}
new_upload
CURRENT_JOB="$JOB"
quota_key=$(python3 "$ROOT/operations.py" quota-key "$CONFIG_DIR/youtubeuploader_client_secrets.json")
(
    exec 7> "$SCRATCH_ROOT/youtube-quota/$quota_key.retry-at.lock"
    flock -n 7
    if upload_job "$JOB"; then echo 'Concurrent upload client lock ignored' >&2; exit 1; fi
    [[ ! -f "$TEST_ROOT/attempts" ]]
)
upload_job "$JOB"
[[ -f "$WORK_DIR/publish/${JOB##*/}/parts/0001.uploaded" ]]
new_upload
FAIL_QUOTA=true
if upload_job "$JOB"; then echo 'Quota failure accepted' >&2; exit 1; fi
[[ ! -f "$JOB/parts/0001.uploaded" ]]
deadline=$(cat "$JOB/upload.retry-at")
[[ "$deadline" -gt $(date +%s) ]]
calls=$(wc -c < "$TEST_ROOT/attempts")
# Another channel sharing the client must not invoke the remote uploader.
CHANNEL=two
pipeline_init
new_upload
if upload_job "$JOB"; then echo 'Shared quota ignored' >&2; exit 1; fi
[[ $(wc -c < "$TEST_ROOT/attempts") -eq "$calls" && $(cat "$JOB/upload.retry-at") -eq "$deadline" ]]
# Receipts allow local handoff even while remote uploads remain paused.
printf 'true\n' > "$JOB/parts/0001.uploaded"
upload_job "$JOB"
[[ -f "$WORK_DIR/publish/${JOB##*/}/parts/0001.uploaded" ]]
new_upload
# Cooldown survives a new worker; changing the client scope permits uploading.
printf '{"installed":{"client_id":"different"}}' > "$CONFIG_DIR/youtubeuploader_client_secrets.json"
FAIL_QUOTA=false
CURRENT_JOB="$JOB"
upload_job "$JOB"
[[ $(wc -c < "$TEST_ROOT/attempts") -gt "$calls" ]]
printf '{"installed":{"client_id":"shared"}}' > "$CONFIG_DIR/youtubeuploader_client_secrets.json"
new_upload
CURRENT_JOB="$JOB"
TEST_NOW=$((deadline + 1))
upload_job "$JOB"
[[ -f "$WORK_DIR/publish/${JOB##*/}/parts/0001.uploaded" ]]
new_upload
CURRENT_JOB="$JOB"
printf '3\n' > "$JOB/segment-index"
python3 - "$JOB/title" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_bytes(('Long title ' * 30).encode() + b'\0')
PY
upload_job "$JOB"
[[ $(cat "$TEST_ROOT/last_title") == *' - part-0003' && $(wc -m < "$TEST_ROOT/last_title") -le 100 ]]
# The per-channel video-count limit uses the same persistent Pacific cooldown.
new_upload
FAIL_QUOTA=true QUOTA_ERROR=uploadLimitExceeded
if upload_job "$JOB"; then echo 'Upload count limit accepted' >&2; exit 1; fi
deadline=$(cat "$JOB/upload.retry-at")
[[ "$deadline" -gt "$TEST_NOW" && ! -f "$JOB/parts/0001.uploaded" ]]
[[ $(cat "$SCRATCH_ROOT/youtube-quota/$quota_key.retry-at") -eq "$deadline" ]]
calls=$(wc -c < "$TEST_ROOT/attempts")
CHANNEL=three
pipeline_init
new_upload
if upload_job "$JOB"; then echo 'Upload count cooldown not shared' >&2; exit 1; fi
[[ $(wc -c < "$TEST_ROOT/attempts") -eq "$calls" && $(cat "$JOB/upload.retry-at") -eq "$deadline" ]]
# Templated description and tags placeholder expansion, and thumbnail argument
TEST_NOW=$((deadline + 1))
FAIL_QUOTA=false
new_upload
CURRENT_JOB="$JOB"
export YOUTUBE_DESCRIPTION='Channel: {channel} | Stream: {stream_url} | Part: {part}/{count} | Title: {title}'
export YOUTUBE_TAGS='{channel},{mode},part-{part}'
printf thumb_data > "$JOB/parts/0001.thumbnail.jpg"
upload_job "$JOB"
[[ $(cat "$TEST_ROOT/last_description") == "Channel: three | Stream: https://twitch.tv/three | Part: 0001/1 | Title: Test upload" ]]
[[ $(cat "$TEST_ROOT/last_tags") == "three,twitch,part-0001" ]]
[[ $(cat "$TEST_ROOT/last_thumbnail") == "$JOB/parts/0001.thumbnail.jpg" ]]
rm -f "$TEST_ROOT/last_thumbnail"
# Test YOUTUBE_UPLOAD_THUMBNAIL=false
new_upload
CURRENT_JOB="$JOB"
printf thumb_data > "$JOB/parts/0001.thumbnail.jpg"
YOUTUBE_UPLOAD_THUMBNAIL=false upload_job "$JOB"
[[ ! -f "$TEST_ROOT/last_thumbnail" ]]
# Test non-segmented long title truncation
new_upload
CURRENT_JOB="$JOB"
python3 - "$JOB/title" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_bytes(('Very long stream title ' * 20).encode() + b'\0')
PY
upload_job "$JOB"
[[ $(wc -m < "$TEST_ROOT/last_title") -le 100 ]]
unset YOUTUBE_DESCRIPTION YOUTUBE_TAGS
printf 'PASS: receipt before notifications and persistent quota pause shared across channels\n'
