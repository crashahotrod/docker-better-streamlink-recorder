#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../entrypoint.sh
source "$ROOT/entrypoint.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=test CLIENT_ID=example CLIENT_SECRET=example USER_NAME=apps
export CONFIG_DIR="$TEST_ROOT/config"
unset UPLOAD ENCODE ENCODE_FALLBACK_CPU CHECK_INTERVAL REMUX YOUTUBE_SPLITTING TWITCH_USER_TOKEN WINDOWS_SAFE_FILENAMES WINDOWS_SAFE_FILENAME
unset ARCHIVE_CHAT HTTP_STATUS_ENABLED HTTP_STATUS_PORT TWITCH_AUDIO_TRACK NOTIFICATION_PROVIDER NOTIFICATION_ON_ERROR
validate_environment
[[ "$UPLOAD" == false && "$ENCODE" == false && "$WINDOWS_SAFE_FILENAMES" == false ]]
[[ "$QUARANTINE_RETENTION_DAYS" == 7 ]]
[[ "$CHECK_INTERVAL" == 30 && "$ENCODE_FALLBACK_CPU" == false ]]
[[ "$ARCHIVE_CHAT" == false && "$HTTP_STATUS_ENABLED" == false && "$HTTP_STATUS_PORT" == 8080 && "$TWITCH_AUDIO_TRACK" == all ]]
for val in all 1 2; do (TWITCH_AUDIO_TRACK="$val"; validate_environment); done
for val in 0 3 invalid; do
    if (TWITCH_AUDIO_TRACK="$val"; validate_environment 2>/dev/null); then echo 'Invalid audio track accepted' >&2; exit 1; fi
done
for val in 1024 8080 65535; do (HTTP_STATUS_PORT="$val"; validate_environment); done
for val in 80 0 65536 abc; do
    if (HTTP_STATUS_PORT="$val"; validate_environment 2>/dev/null); then echo 'Invalid status port accepted' >&2; exit 1; fi
done
if (NOTIFICATION_PROVIDER=unknown; validate_environment 2>/dev/null); then echo 'Unknown notification provider accepted' >&2; exit 1; fi
# Invalid configuration stays local to each validation attempt.
# shellcheck disable=SC2030
if (NOTIFICATION_PROVIDER=gotify; unset NOTIFICATION_URL NOTIFICATION_TOKEN; validate_environment 2>/dev/null); then exit 1; fi
# shellcheck disable=SC2031
(export NOTIFICATION_PROVIDER=ntfy NOTIFICATION_URL=https://example.invalid/topic; validate_environment)
for val in 5 10 30 600; do (CHECK_INTERVAL="$val"; validate_environment; [[ "$CHECK_INTERVAL" == "$val" ]]); done
for val in 0 4 -1 abc 1.5 05; do
    if (CHECK_INTERVAL="$val"; validate_environment 2>/dev/null); then echo "Invalid CHECK_INTERVAL $val accepted" >&2; exit 1; fi
done
(unset WINDOWS_SAFE_FILENAMES; WINDOWS_SAFE_FILENAME=true; validate_environment; [[ "$WINDOWS_SAFE_FILENAMES" == true ]])
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
for variable in UPLOAD ENCODE ENCODE_FALLBACK_CPU ARCHIVE_CHAT STREAM_CHAPTERS HTTP_STATUS_ENABLED NOTIFICATION_ON_ERROR REMUX YOUTUBE_SPLITTING AUTO_RECOVER_INTERRUPTED YOUTUBE_UPLOAD_THUMBNAIL WINDOWS_SAFE_FILENAMES; do
    if (export "$variable=invalid"; validate_environment 2>/dev/null); then
        echo "Invalid $variable accepted" >&2; exit 1
    fi
done
for val in 0 14 abc 1.5; do
    if (CHAPTER_POLL_INTERVAL="$val"; validate_environment 2>/dev/null); then echo 'Invalid chapter interval accepted' >&2; exit 1; fi
done
for setting in YOUTUBE_THUMBNAIL_TIMESTAMP=bad DISCORD_PING_ROLE_ID=abc DISCORD_THREAD_ID=-1 RCLONE_RETRIES=0 RCLONE_BWLIMIT=fast; do
    if (export "${setting?}"; validate_environment 2>/dev/null); then echo "Invalid $setting accepted" >&2; exit 1; fi
done
for val in -1 0 abc; do
    if (PUID="$val"; configure_user_ids 2>/dev/null); then echo "Invalid PUID $val accepted" >&2; exit 1; fi
    if (PGID="$val"; configure_user_ids 2>/dev/null); then echo "Invalid PGID $val accepted" >&2; exit 1; fi
done
for YOUTUBE_PRIVACY in public private unlisted; do validate_environment; done
if (YOUTUBE_PRIVACY=invalid; validate_environment 2>/dev/null); then exit 1; fi
YOUTUBE_PRIVACY=public
for val in 0 18 23 51; do (ENCODE_CRF="$val"; validate_environment); done
for val in -1 52 abc; do
    if (ENCODE_CRF="$val"; validate_environment 2>/dev/null); then echo "Invalid ENCODE_CRF $val accepted" >&2; exit 1; fi
done
for accel in auto cpu nvenc vaapi qsv; do (ENCODE_ACCEL="$accel"; unset ENCODE_PRESET ENCODE_VIDEO_CODEC; validate_environment); done
if (ENCODE_ACCEL=unknown; validate_environment 2>/dev/null); then echo 'Invalid accelerator accepted' >&2; exit 1; fi
if (ENCODE_ACCEL=nvenc; ENCODE_VIDEO_CODEC=libx264; validate_environment 2>/dev/null); then echo 'Encoder mismatch accepted' >&2; exit 1; fi
if (ENCODE_ACCEL=vaapi; ENCODE_PRESET=medium; validate_environment 2>/dev/null); then echo 'VAAPI preset accepted' >&2; exit 1; fi
for val in 0 1 23 51; do (ENCODE_QUALITY="$val"; validate_environment); done
for val in -1 52 abc 1.5; do
    if (ENCODE_QUALITY="$val"; validate_environment 2>/dev/null); then echo "Invalid quality $val accepted" >&2; exit 1; fi
done
if (ENCODE_ACCEL=qsv; ENCODE_QUALITY=0; validate_environment 2>/dev/null); then echo 'QSV zero quality accepted' >&2; exit 1; fi
if (ENCODE=true; ENCODE_ACCEL=vaapi; ENCODE_DEVICE="$TEST_ROOT/missing-render-node"; prepare_encoding_device 2>/dev/null); then
    echo 'Missing render device accepted' >&2; exit 1
fi
(ENCODE=true; ENCODE_ACCEL=cpu; ENCODE_DEVICE="$TEST_ROOT/missing-render-node"; prepare_encoding_device)
(ENCODE=true; ENCODE_ACCEL=vaapi; ENCODE_FALLBACK_CPU=true; ENCODE_DEVICE="$TEST_ROOT/missing-render-node"; prepare_encoding_device)
for val in 0 5 100; do (MIN_FREE_SPACE_GB="$val"; validate_environment); done
for val in -1 abc; do
    if (MIN_FREE_SPACE_GB="$val"; validate_environment 2>/dev/null); then echo "Invalid MIN_FREE_SPACE_GB $val accepted" >&2; exit 1; fi
done
for val in 0 7 30; do (RETENTION_DAYS="$val"; validate_environment); done
for val in -1 abc; do
    if (RETENTION_DAYS="$val"; validate_environment 2>/dev/null); then echo "Invalid RETENTION_DAYS $val accepted" >&2; exit 1; fi
done
for val in 0 7 30; do (QUARANTINE_RETENTION_DAYS="$val"; validate_environment); done
for val in -1 abc 1.5; do
    if (QUARANTINE_RETENTION_DAYS="$val"; validate_environment 2>/dev/null); then echo "Invalid QUARANTINE_RETENTION_DAYS $val accepted" >&2; exit 1; fi
done
for val in 150 180 600; do (RECORDER_STALE_SECONDS="$val"; validate_environment); done
for val in 1 21600 42900; do (YOUTUBE_SEGMENT_SECONDS="$val"; validate_environment); done
for val in 0 -1 42901 invalid 00006; do
    if (YOUTUBE_SEGMENT_SECONDS="$val"; validate_environment 2>/dev/null); then echo "Invalid segment duration $val accepted" >&2; exit 1; fi
done
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
