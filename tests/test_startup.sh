#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../entrypoint.sh
source "$ROOT/entrypoint.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=test CLIENT_ID=example CLIENT_SECRET=example USER_NAME=apps
export CONFIG_DIR="$TEST_ROOT/config"
unset UPLOAD ENCODE REMUX YOUTUBE_SPLITTING TWITCH_USER_TOKEN
validate_environment
[[ "$UPLOAD" == false && "$ENCODE" == false ]]
validate_upload_credentials
for UPLOAD in false true; do
    for ENCODE in false true; do
        UPLOAD_BOT_NAME='test'
        validate_environment
        [[ "$UPLOAD" == true || "$UPLOAD" == false ]]
        [[ "$ENCODE" == true || "$ENCODE" == false ]]
    done
done
# Invalid settings intentionally stay local to each validation attempt.
# shellcheck disable=SC2030
if (MODE=unsupported; validate_environment); then exit 1; fi
# shellcheck disable=SC2030
if (CHANNEL=../outside; validate_environment); then exit 1; fi
# shellcheck disable=SC2030
if (ENCODE=invalid; validate_environment); then exit 1; fi
if (UPLOAD=true; unset UPLOAD_BOT_NAME; validate_environment); then exit 1; fi
gosu() { shift; "$@"; }
UPLOAD=true
if validate_upload_credentials; then echo 'Missing credentials accepted' >&2; exit 1; fi
mkdir -- "$CONFIG_DIR"
touch "$CONFIG_DIR/youtubeuploader_client_secrets.json" "$CONFIG_DIR/youtubeuploader_request.token"
validate_upload_credentials

# shellcheck source=../download.sh
source "$ROOT/download.sh"
build_streamlink_args
[[ "${STREAMLINK_ARGS[*]}" != *Authorization* ]]
TWITCH_USER_TOKEN='example token'
build_streamlink_args
found=false
for arg in "${STREAMLINK_ARGS[@]}"; do
    if [[ "$arg" == 'Authorization=OAuth example token' ]]; then found=true; fi
done
[[ "$found" == true ]]
MODE=kick
build_streamlink_args
[[ "${STREAMLINK_ARGS[*]}" != *Authorization* ]]
# Unauthorized live checks invalidate the parent shell's cached app token.
refresh_token() { :; }
fetch_stream_info() { return 2; }
TOKEN_EXPIRES_AT=9999999999
if monitor_once; then exit 1; fi
[[ "$TOKEN_EXPIRES_AT" -eq 0 ]]
printf 'PASS: startup flags, validation, optional Twitch token, credential gating\n'
