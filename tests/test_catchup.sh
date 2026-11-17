#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../catchup_vod.sh
source "$ROOT/catchup_vod.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=test CLIENT_ID=example CLIENT_SECRET=example
export SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage" MIN_FREE_SPACE_GB=0
export UPLOAD=false ENCODE=false REMUX=true YOUTUBE_SPLITTING=true ARCHIVE_CHAT=true STREAM_CHAPTERS=true
mkdir -p "$STORAGE_DIR" "$TEST_ROOT/bin"
export PATH="$TEST_ROOT/bin:$PATH"
cat > "$TEST_ROOT/bin/streamlink" <<'PY'
#!/usr/bin/env python3
import os
from pathlib import Path
import sys
assert os.environ['ARCHIVE_CHAT'] == 'false'
assert os.environ['STREAM_CHAPTERS'] == 'false'
assert sys.argv[-2] == 'https://www.twitch.tv/videos/123'
output = Path(sys.argv[sys.argv.index('--output') + 1])
output.write_bytes(b'VOD fixture original')
PY
chmod +x "$TEST_ROOT/bin/streamlink"
curl() {
    if [[ "$*" == *oauth2/token* ]]; then
        printf '{"access_token":"fixture","expires_in":3600}'
    else
        printf '{"data":[{"id":"123","user_login":"test","title":"Past broadcast","created_at":"2024-04-15T12:30:00Z"}]}'
    fi
}
pipeline_init
new_recording_job 'Current live stream' test live
live_job="$RECORDING_JOB"
live_pointer=$(cat "$WORK_DIR/active.job")
catchup_main 123
[[ $(cat "$WORK_DIR/active.job") == "$live_pointer" && -d "$live_job" ]]
job=$(find "$WORK_DIR/normalize" -mindepth 1 -maxdepth 1 -type d -name 'recording.*')
[[ -f "$job/capture.bin" && $(cat "$job/vod-id") == 123 ]]
[[ $(cat "$job/season") == 202404 && $(cat "$job/recorded-date") == 04/15/2024 ]]
[[ $(cat "$job/upload-required") == false && $(cat "$job/encode-required") == false && $(cat "$job/remux-required") == true ]]
catchup_main 123
[[ $(find "$WORK_DIR/normalize" -mindepth 1 -maxdepth 1 -type d | wc -l) == 1 ]]
if catchup_main bad 2>/dev/null; then echo 'Invalid VOD ID accepted' >&2; exit 1; fi
# A failed/interrupted writer remains separate from the active live capture.
cat > "$TEST_ROOT/bin/streamlink" <<'PY'
#!/usr/bin/env python3
from pathlib import Path
import sys
Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(b'partial VOD retained')
raise SystemExit(1)
PY
chmod +x "$TEST_ROOT/bin/streamlink"
rm "$WORK_DIR/vods/123.state"
if catchup_main 123; then echo 'Failed VOD capture accepted' >&2; exit 1; fi
AUTO_RECOVER_INTERRUPTED=true recover_completed_recordings
[[ $(find "$WORK_DIR/active" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' | wc -l) == 2 ]]
if catchup_main 123 2>/dev/null; then echo 'Partial VOD silently overwritten' >&2; exit 1; fi
printf 'PASS: VOD catch-up dates, queues, live isolation, idempotency and failure retention\n'
