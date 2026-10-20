#!/bin/bash
# Producers write only to the local outbox; this test permits no HTTP calls.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../download.sh
source "$ROOT/download.sh"
# shellcheck source=../status.sh
source "$ROOT/status.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=test SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
export REMUX=false UPLOAD=false ENCODE=false
export DISCORD_WEBHOOK_URL=https://example.invalid/public/secret
export DISCORD_MONITOR_WEBHOOK_URL=https://example.invalid/monitor/secret
export DISCORD_NOTIFY_ON_ERROR=true
pipeline_init
curl() { echo 'Unexpected network call from producer' >&2; exit 99; }
message=$'**test is LIVE!**\nTitle: "quotes" \\ backslash caf\u00e9\nnext line'
notify_discord "$message"
files=("$WORK_DIR"/notifications/event.*.json)
jq -e --arg expected "$message" '.payload.content == $expected and .payload.allowed_mentions.parse == [] and .target == "public"' "${files[0]}" > /dev/null
find "$WORK_DIR/notifications" -type f -delete
streamlink() {
    while (($#)); do
        if [[ "$1" == --output ]]; then printf fixture > "$2"; return 0; fi
        shift
    done
    return 1
}
record_stream "$message" test 123
[[ -f "$WORK_DIR/normalize/${RECORDING_JOB##*/}/completed" ]]
[[ $(find "$WORK_DIR/notifications" -name 'event.*.json' | wc -l) -eq 2 ]]
# Failure to queue alerts cannot prevent recording completion.
queue_discord() { return 1; }
record_stream no-notifications test 124
[[ -f "$WORK_DIR/normalize/${RECORDING_JOB##*/}/completed" ]]
# Dashboard delivery requires a dedicated webhook; alerts retain their fallback.
unset DISCORD_MONITOR_WEBHOOK_URL
[[ $(get_monitor_webhook) == "$DISCORD_WEBHOOK_URL" ]]
DISCORD_NOTIFY_ON_ERROR=false
[[ -z $(get_monitor_webhook) ]]
# A stale active pointer or state file cannot report a running recorder.
new_recording_job stale test 125
printf fixture > "$RECORDING_JOB/capture.bin"
set_channel_status recording
[[ $(get_channel_status) == offline ]]
[[ $(build_status_json | jq -r '.active_capture') == null ]]
exec 9> "$WORK_DIR/download.lock"
flock -n 9
[[ $(get_channel_status) == recording ]]
json=$(build_status_json)
jq -e '.active_capture.stalled == false and .active_capture.last_growth_at > 0 and .active_capture.segment_index == null' <<< "$json" > /dev/null
# Segmented stream status tracks child segment and overall broadcast duration.
printf '2\n' > "$RECORDING_JOB/segment-index"
printf 'parent123\n' > "$RECORDING_JOB/segment-parent"
mkdir -p "$WORK_DIR/sessions/recording.parent123"
printf '%s\n' "$(( $(date +%s) - 3600 ))" > "$WORK_DIR/sessions/recording.parent123/started-at"
json=$(build_status_json)
jq -e '.active_capture.segment_index == 2 and .active_capture.total_duration_seconds >= 3600 and (.active_capture.total_duration_human | test("^[0-9]{2}:[0-9]{2}:[0-9]{2}$"))' <<< "$json" > /dev/null
# No capture growth for longer than the threshold is an error.
touch -d '10 minutes ago' "$RECORDING_JOB/capture.bin"
[[ $(get_channel_status) == error ]]
[[ $(build_status_json | jq -r '.active_capture.stalled') == true ]]
rm "$WORK_DIR/active.job"
set_channel_status monitoring
[[ $(get_channel_status) == monitoring ]]
write_state "$WORK_DIR/recorder.heartbeat" "$(( $(date +%s) - 600 ))"
[[ $(get_channel_status) == error ]]
set_channel_status offline
[[ $(get_channel_status) == offline ]]
set_channel_status error
[[ $(get_channel_status) == error ]]
flock -u 9
exec 9>&-
[[ $(get_channel_status) == offline ]]
status_cycle
[[ -f "$SCRATCH_ROOT/status-test-twitch.json" && -f "$WORK_DIR/status.json" ]]
CHANNEL=another
pipeline_init
status_cycle
MODE=kick CHANNEL=test
pipeline_init
status_cycle
jq -e '.channel == "test" and .mode == "twitch"' "$SCRATCH_ROOT/status-test-twitch.json" > /dev/null
jq -e '.channel == "another" and .mode == "twitch"' "$SCRATCH_ROOT/status-another-twitch.json" > /dev/null
jq -e '.channel == "test" and .mode == "kick"' "$SCRATCH_ROOT/status-test-kick.json" > /dev/null
[[ ! -f "$SCRATCH_ROOT/status.json" ]]
printf 'PASS: local notification producers, failed enqueue isolation, live recorder and stalled-capture status\n'
