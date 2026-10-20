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
for variable in MODE CHANNEL CLIENT_ID CLIENT_SECRET USER_NAME; do
    if (unset "$variable"; validate_environment 2>/dev/null); then
        echo "Missing $variable accepted" >&2; exit 1
    fi
done
for variable in UPLOAD ENCODE REMUX YOUTUBE_SPLITTING AUTO_RECOVER_INTERRUPTED; do
    if (export "$variable=invalid"; validate_environment 2>/dev/null); then
        echo "Invalid $variable accepted" >&2; exit 1
    fi
done
for YOUTUBE_PRIVACY in public private unlisted; do validate_environment; done
if (YOUTUBE_PRIVACY=invalid; validate_environment 2>/dev/null); then exit 1; fi
YOUTUBE_PRIVACY=public
for val in 0 18 23 51; do (ENCODE_CRF="$val"; validate_environment); done
for val in -1 52 abc; do
    if (ENCODE_CRF="$val"; validate_environment 2>/dev/null); then echo "Invalid ENCODE_CRF $val accepted" >&2; exit 1; fi
done
for val in 0 5 100; do (MIN_FREE_SPACE_GB="$val"; validate_environment); done
for val in -1 abc; do
    if (MIN_FREE_SPACE_GB="$val"; validate_environment 2>/dev/null); then echo "Invalid MIN_FREE_SPACE_GB $val accepted" >&2; exit 1; fi
done
for val in 0 7 30; do (RETENTION_DAYS="$val"; validate_environment); done
for val in -1 abc; do
    if (RETENTION_DAYS="$val"; validate_environment 2>/dev/null); then echo "Invalid RETENTION_DAYS $val accepted" >&2; exit 1; fi
done
for val in 150 180 600; do (RECORDER_STALE_SECONDS="$val"; validate_environment); done
for val in 0 149 -1 abc; do
    if (RECORDER_STALE_SECONDS="$val"; validate_environment 2>/dev/null); then echo "Invalid RECORDER_STALE_SECONDS $val accepted" >&2; exit 1; fi
done
gosu() { shift; "$@"; }
UPLOAD=true
if validate_upload_credentials; then echo 'Missing credentials accepted' >&2; exit 1; fi
mkdir -- "$CONFIG_DIR"
touch "$CONFIG_DIR/youtubeuploader_client_secrets.json" "$CONFIG_DIR/youtubeuploader_request.token"
validate_upload_credentials
for filename in youtubeuploader_client_secrets.json youtubeuploader_request.token; do
    mv -- "$CONFIG_DIR/$filename" "$CONFIG_DIR/$filename.saved"
    if validate_upload_credentials 2>/dev/null; then echo "Missing $filename accepted" >&2; exit 1; fi
    mv -- "$CONFIG_DIR/$filename.saved" "$CONFIG_DIR/$filename"
done
gosu() {
    if [[ "${3:-}" == -w ]]; then return 1; fi
    shift; "$@"
}
if validate_upload_credentials 2>/dev/null; then echo "Unwritable token accepted" >&2; exit 1; fi
gosu() { shift; "$@"; }
printf 'PASS: required settings, all boolean flags, privacy choices and credential gating\n'
