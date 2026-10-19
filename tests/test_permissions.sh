#!/bin/bash
# Run as root in Linux/CI, against an existing user with a same-named group.
set -euo pipefail
[[ "$EUID" -eq 0 ]] || { echo 'Run this check as root with TEST_APP_USER set.' >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../entrypoint.sh
source "$ROOT/entrypoint.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
chmod 755 -- "$TEST_ROOT"
# Hosted runners may keep their checkout behind a private home directory.
# Stage only the library needed by the app user, without changing checkout ACLs.
mkdir -- "$TEST_ROOT/tools"
chmod 755 -- "$TEST_ROOT/tools"
cp -- "$ROOT/pipeline.sh" "$TEST_ROOT/tools/pipeline.sh"
chmod 644 -- "$TEST_ROOT/tools/pipeline.sh"
export MODE=twitch CHANNEL=test CLIENT_ID=example CLIENT_SECRET=example
export USER_NAME="${TEST_APP_USER:?Set TEST_APP_USER to an existing non-root user}"
[[ $(id -u "$USER_NAME") -ne 0 ]]
export SCRATCH_ROOT="$TEST_ROOT/scratch" STORAGE_DIR="$TEST_ROOT/storage"
export CONFIG_DIR="$TEST_ROOT/config" LOG_DIR="$TEST_ROOT/log" DBUS_RUN_DIR="$TEST_ROOT/dbus"
validate_environment
prepare_directories
validate_upload_credentials
gosu "$USER_NAME" bash -s -- "$TEST_ROOT/tools" <<'SH'
set -euo pipefail
source "$1/pipeline.sh"
pipeline_init
new_recording_job title test 1
printf 'preserved' > "$RECORDING_JOB/media.ts"
SH
# Reinitialization preserves pending media and repairs ownership on new paths.
prepare_directories
for stage in active normalize quarantine upload encode publish; do
    gosu "$USER_NAME" test -w "$WORK_DIR/$stage"
done
[[ $(find "$WORK_DIR/active" -name media.ts -exec cat {} \;) == preserved ]]
UPLOAD=true
touch "$CONFIG_DIR/youtubeuploader_client_secrets.json" "$CONFIG_DIR/youtubeuploader_request.token"
chown "$USER_NAME:$USER_NAME" "$CONFIG_DIR"/*
for filename in youtubeuploader_client_secrets.json youtubeuploader_request.token; do
    chmod 000 -- "$CONFIG_DIR/$filename"
    if validate_upload_credentials 2>/dev/null; then echo "Unreadable $filename accepted" >&2; exit 1; fi
    chmod 600 -- "$CONFIG_DIR/$filename"
done
validate_upload_credentials
printf 'PASS: fresh/reused directories writable as application user; media preserved\n'
