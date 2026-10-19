#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../download.sh
source "$ROOT/download.sh"
# shellcheck source=../status.sh
source "$ROOT/status.sh"

TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
trap 'echo "Error on line $LINENO: command was $BASH_COMMAND" >&2' ERR

# --- Test 1: Basic Discord escaping & safe HTTP failure handling ---
DISCORD_WEBHOOK_URL=https://example.invalid/secret
LAST_PAYLOAD=""
LAST_URL=""
LAST_METHOD=""
CURL_STATUS_CODE=200
CURL_RESPONSE_BODY='{"id": "msg-default"}'

curl() {
    local method="GET" data="" url=""
    while (($#)); do
        case "$1" in
            -X) method="$2"; shift 2 ;;
            --data) data="$2"; shift 2 ;;
            *)
                if [[ "$1" =~ ^https?:// ]]; then url="$1"; fi
                shift
                ;;
        esac
    done
    LAST_METHOD="$method"
    LAST_PAYLOAD="$data"
    LAST_URL="$url"
    printf '%s' "$method" > "$TEST_ROOT/last_method"
    printf '%s' "$url" > "$TEST_ROOT/last_url"
    printf '%s' "$data" > "$TEST_ROOT/payload.json"
    if [[ "${FAIL_WEBHOOK:-false}" == true ]]; then
        return 1
    fi
    printf '%s\n%s\n' "$CURL_RESPONSE_BODY" "$CURL_STATUS_CODE"
    return 0
}

message=$'**test is LIVE!**\nTitle: "quotes" \\ backslash café\nnext line'
notify_discord "$message"
jq -e --arg expected "$message" '.content == $expected' "$TEST_ROOT/payload.json" > /dev/null

FAIL_WEBHOOK=true
if notify_discord "$message" 2> "$TEST_ROOT/error"; then exit 1; fi
[[ $(cat "$TEST_ROOT/error") != *secret* ]]
grep -q 'recording will continue' "$TEST_ROOT/error"
FAIL_WEBHOOK=false

# A failed notification must not prevent completion of the recording itself.
export MODE=twitch CHANNEL=test SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
export REMUX=false UPLOAD=false ENCODE=false
pipeline_init
streamlink() {
    while (($#)); do
        if [[ "$1" == --output ]]; then printf 'fixture' > "$2"; return 0; fi
        shift
    done
    return 1
}
record_stream "$message" test 123
[[ -f "$WORK_DIR/normalize/${RECORDING_JOB##*/}/completed" ]]
printf 'PASS: Discord JSON escaping and safe HTTP/transport failure handling\n'

# --- Test 2: Monitor Webhook Resolution & Error Notification Flag ---
export DISCORD_MONITOR_WEBHOOK_URL="https://discord.com/api/webhooks/monitor/dedicated"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/main/general"
export DISCORD_NOTIFY_ON_ERROR=true

[[ "$(get_monitor_webhook)" == "https://discord.com/api/webhooks/monitor/dedicated" ]]

# Fallback to main webhook when monitor webhook is unset and DISCORD_NOTIFY_ON_ERROR is true
unset DISCORD_MONITOR_WEBHOOK_URL
[[ "$(get_monitor_webhook)" == "https://discord.com/api/webhooks/main/general" ]]

# When DISCORD_NOTIFY_ON_ERROR is false and no monitor webhook is set, resolve empty
DISCORD_NOTIFY_ON_ERROR=false
[[ -z "$(get_monitor_webhook)" ]]

printf 'PASS: get_monitor_webhook precedence and fallback behavior\n'

# --- Test 3: Operational Failure & Event Alerts ---
DISCORD_MONITOR_WEBHOOK_URL="https://discord.com/api/webhooks/monitor/dedicated"
DISCORD_NOTIFY_ON_ERROR=true
mkdir -p -- "$WORK_DIR"

# 3a. Recording Completed Alert
sample_job="$WORK_DIR/active/recording.test1234"
mkdir -p -- "$sample_job"
printf 'Completed Gaming Stream\0' > "$sample_job/title"
printf '%s\n' "$(( $(date +%s) - 3665 ))" > "$sample_job/started-at"
printf 'sample media capture data 1234567890' > "$sample_job/capture.bin"

notify_recording_completed "$sample_job"
[[ "$LAST_METHOD" == "POST" ]]
[[ "$LAST_URL" == "https://discord.com/api/webhooks/monitor/dedicated" ]]
jq -e '.embeds[0].title | contains("Recording Completed")' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Duration" and (.value | test("^01:01:"))) ' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Stream Title" and .value == "Completed Gaming Stream")' "$TEST_ROOT/payload.json" > /dev/null

# 3b. Low Disk Space Alert & Debounce Check
notify_low_disk_space "$TEST_ROOT/storage" 3 10
jq -e '.embeds[0].title | contains("Low Disk Space")' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Available Space" and .value == "3 GB")' "$TEST_ROOT/payload.json" > /dev/null

# Immediate second call should be debounced
LAST_PAYLOAD=""
notify_low_disk_space "$TEST_ROOT/storage" 3 10
[[ -z "$LAST_PAYLOAD" ]]

# 3c. Job Quarantined Alert
quarantine_sample="$WORK_DIR/quarantine/recording.corrupt99"
mkdir -p -- "$quarantine_sample"
printf 'Corrupted Stream Test\0' > "$quarantine_sample/title"

notify_job_quarantined "$quarantine_sample"
jq -e '.embeds[0].title | contains("Job Quarantined")' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Job ID" and .value == "recording.corrupt99")' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Stream Title" and .value == "Corrupted Stream Test")' "$TEST_ROOT/payload.json" > /dev/null

# 3d. YouTube Quota Exceeded Alert
notify_youtube_quota_exceeded 8 1729324800
jq -e '.embeds[0].title | contains("YouTube API Quota Exceeded")' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Backoff Duration" and .value == "~8 hours")' "$TEST_ROOT/payload.json" > /dev/null
jq -e '.embeds[0].fields[] | select(.name == "Uploads Resume" and .value == "08:00 UTC")' "$TEST_ROOT/payload.json" > /dev/null

printf 'PASS: Operational alerts (recording completed, low disk space debouncing, quarantine, youtube quota)\n'

# --- Test 4: JSON Status Exporter (status.json) ---
write_state "$WORK_DIR/channel.status" "monitoring"
status_json=$(build_status_json)
[[ $(jq -r '.status' <<< "$status_json") == "monitoring" ]]
[[ $(jq -r '.channel' <<< "$status_json") == "test" ]]
[[ $(jq -r '.mode' <<< "$status_json") == "twitch" ]]
[[ $(jq -r '.active_capture' <<< "$status_json") == "null" ]]
[[ $(jq -r '.queues.quarantine' <<< "$status_json") -ge 1 ]]

# When recording is active
write_state "$WORK_DIR/active.job" "$sample_job"
write_state "$WORK_DIR/channel.status" "recording"
rec_status_json=$(build_status_json)
[[ $(jq -r '.status' <<< "$rec_status_json") == "recording" ]]
[[ $(jq -r '.active_capture.job_id' <<< "$rec_status_json") == "recording.test1234" ]]
[[ $(jq -r '.active_capture.title' <<< "$rec_status_json") == "Completed Gaming Stream" ]]
[[ $(jq -r '.active_capture.duration_human' <<< "$rec_status_json") =~ ^01:01: ]]

# Write status files atomically and verify both locations
write_status_files "$rec_status_json"
[[ -f "$SCRATCH_ROOT/status.json" ]]
[[ -f "$WORK_DIR/status.json" ]]
[[ $(jq -r '.status' "$SCRATCH_ROOT/status.json") == "recording" ]]
[[ $(jq -r '.status' "$WORK_DIR/status.json") == "recording" ]]

printf 'PASS: status.json structure, channel status tracking, and atomic persistence\n'

# --- Test 5: Persistent Discord Status Message Lifecycle ---
STATUS_ID_FILE="$WORK_DIR/status_message.id"
rm -f -- "$STATUS_ID_FILE" "$WORK_DIR/.last_discord_status_time" "$WORK_DIR/.last_discord_status_fp"

# 5a. Initial Creation (POST ?wait=true)
CURL_STATUS_CODE=200
CURL_RESPONSE_BODY='{"id": "discord-msg-10001"}'
export STATUS_ONCE=true

update_persistent_discord "$rec_status_json"
[[ -f "$STATUS_ID_FILE" ]]
[[ "$(cat "$STATUS_ID_FILE")" == "discord-msg-10001" ]]
[[ "$(cat "$TEST_ROOT/last_method")" == "POST" ]]
[[ "$(cat "$TEST_ROOT/last_url")" == *"wait=true"* ]]

# 5b. Subsequent Update (PATCH /messages/<id>)
CURL_STATUS_CODE=200
CURL_RESPONSE_BODY='{"id": "discord-msg-10001"}'

update_persistent_discord "$rec_status_json"
[[ "$(cat "$STATUS_ID_FILE")" == "discord-msg-10001" ]]
[[ "$(cat "$TEST_ROOT/last_method")" == "PATCH" ]]
[[ "$(cat "$TEST_ROOT/last_url")" == "https://discord.com/api/webhooks/monitor/dedicated/messages/discord-msg-10001" ]]

# 5c. Message Deleted Recovery (PATCH returns 404 -> recreate via POST ?wait=true)
curl() {
    local method="GET" data="" url=""
    while (($#)); do
        case "$1" in
            -X) method="$2"; shift 2 ;;
            --data) data="$2"; shift 2 ;;
            *)
                if [[ "$1" =~ ^https?:// ]]; then url="$1"; fi
                shift
                ;;
        esac
    done
    LAST_METHOD="$method"
    LAST_PAYLOAD="$data"
    LAST_URL="$url"
    printf '%s' "$method" > "$TEST_ROOT/last_method"
    printf '%s' "$url" > "$TEST_ROOT/last_url"
    printf '%s' "$data" > "$TEST_ROOT/payload.json"
    if [[ "$method" == "PATCH" ]]; then
        # Simulate deleted message returning 404
        printf '{"message": "Unknown Message", "code": 10008}\n404\n'
    else
        # Simulate recreation via POST
        printf '{"id": "discord-msg-20002"}\n200\n'
    fi
    return 0
}

update_persistent_discord "$rec_status_json"
[[ -f "$STATUS_ID_FILE" ]]
[[ "$(cat "$STATUS_ID_FILE")" == "discord-msg-20002" ]]
[[ "$(cat "$TEST_ROOT/last_method")" == "POST" ]]
[[ "$(cat "$TEST_ROOT/last_url")" == *"wait=true"* ]]

# 5d. Discrete Alerts Do Not Modify status_message.id
notify_low_disk_space "$TEST_ROOT/storage" 1 10 || true
[[ "$(cat "$STATUS_ID_FILE")" == "discord-msg-20002" ]]

printf 'PASS: Persistent Discord status message creation, patch updates, and 404 deletion recreation\n'
