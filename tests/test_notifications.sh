#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../download.sh
source "$ROOT/download.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
DISCORD_WEBHOOK_URL=https://example.invalid/secret
curl() {
    while (($#)); do
        if [[ "$1" == --data ]]; then printf '%s' "$2" > "$TEST_ROOT/payload.json"; fi
        shift
    done
    [[ "${FAIL_WEBHOOK:-false}" != true ]]
}
message=$'**test is LIVE!**\nTitle: "quotes" \\ backslash café\nnext line'
notify_discord "$message"
jq -e --arg expected "$message" '.content == $expected' "$TEST_ROOT/payload.json" > /dev/null
FAIL_WEBHOOK=true
if notify_discord "$message" 2> "$TEST_ROOT/error"; then exit 1; fi
[[ $(cat "$TEST_ROOT/error") != *secret* ]]
grep -q 'recording will continue' "$TEST_ROOT/error"
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
ffprobe() { printf '{"streams":[{"codec_type":"video"}]}\n'; }
record_stream "$message" test 123
[[ -f "$WORK_DIR/normalize/${RECORDING_JOB##*/}/completed" ]]
printf 'PASS: Discord JSON escaping and safe HTTP/transport failure handling\n'
