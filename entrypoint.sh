#!/bin/bash
set -euo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

validate_environment() {
    local name value errors=0
    for name in MODE CHANNEL CLIENT_ID CLIENT_SECRET USER_NAME; do
        if [[ -z "${!name:-}" ]]; then
            printf 'ERROR: Required environment variable %s is not set.\n' "$name" >&2
            errors=1
        fi
    done
    case "${MODE:-}" in twitch|kick) ;; *) echo 'ERROR: MODE must be twitch or kick.' >&2; errors=1 ;; esac
    if [[ ! "${CHANNEL:-}" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_-]*$ ]]; then
        echo 'ERROR: CHANNEL must be a channel slug without path separators.' >&2
        errors=1
    fi
    if [[ -n "${WINDOWS_SAFE_FILENAME:-}" && -z "${WINDOWS_SAFE_FILENAMES:-}" ]]; then
        export WINDOWS_SAFE_FILENAMES="$WINDOWS_SAFE_FILENAME"
    fi
    for name in UPLOAD ENCODE ENCODE_FALLBACK_CPU REMUX YOUTUBE_SPLITTING AUTO_RECOVER_INTERRUPTED WINDOWS_SAFE_FILENAMES; do
        value="${!name:-false}"
        case "$value" in true|false) ;; *) printf 'ERROR: %s must be true or false.\n' "$name" >&2; errors=1 ;; esac
        export "$name=$value"
    done
    value="${YOUTUBE_UPLOAD_THUMBNAIL:-true}"
    case "$value" in true|false) ;; *) printf 'ERROR: YOUTUBE_UPLOAD_THUMBNAIL must be true or false.\n' >&2; errors=1 ;; esac
    export YOUTUBE_UPLOAD_THUMBNAIL="$value"
    value="${DISCORD_NOTIFY_ON_ERROR:-true}"
    case "$value" in true|false) ;; *) printf 'ERROR: DISCORD_NOTIFY_ON_ERROR must be true or false.\n' >&2; errors=1 ;; esac
    export DISCORD_NOTIFY_ON_ERROR="$value"
    if [[ "$UPLOAD" == true && -z "${UPLOAD_BOT_NAME:-}" ]]; then
        echo 'ERROR: UPLOAD_BOT_NAME is required when uploading.' >&2
        errors=1
    fi
    case "${YOUTUBE_PRIVACY:-public}" in
        public|private|unlisted) ;;
        *) echo 'ERROR: YOUTUBE_PRIVACY must be public, private, or unlisted.' >&2; errors=1 ;;
    esac
    if [[ -n "${ENCODE_CRF:-}" ]]; then
        if [[ ! "$ENCODE_CRF" =~ ^[0-9]+$ ]] || (( ENCODE_CRF < 0 || ENCODE_CRF > 51 )); then
            echo 'ERROR: ENCODE_CRF must be an integer between 0 and 51.' >&2
            errors=1
        fi
    fi
    build_encoding_args || errors=1
    value="${CHECK_INTERVAL:-30}"
    if [[ ! "$value" =~ ^([5-9]|[1-9][0-9]+)$ ]]; then
        echo 'ERROR: CHECK_INTERVAL must be an integer of at least 5 seconds.' >&2
        errors=1
    fi
    export CHECK_INTERVAL="$value"
    if [[ -n "${MIN_FREE_SPACE_GB:-}" ]]; then
        if [[ ! "$MIN_FREE_SPACE_GB" =~ ^[0-9]+$ ]]; then
            echo 'ERROR: MIN_FREE_SPACE_GB must be a non-negative integer.' >&2
            errors=1
        fi
    fi
    if [[ -n "${RETENTION_DAYS:-}" ]]; then
        if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
            echo 'ERROR: RETENTION_DAYS must be a non-negative integer.' >&2
            errors=1
        fi
    fi
    value="${QUARANTINE_RETENTION_DAYS:-7}"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        echo 'ERROR: QUARANTINE_RETENTION_DAYS must be a non-negative integer.' >&2
        errors=1
    fi
    export QUARANTINE_RETENTION_DAYS="$value"
    value="${RECORDER_STALE_SECONDS:-180}"
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]] || (( value < 150 )); then
        echo 'ERROR: RECORDER_STALE_SECONDS must be an integer of at least 150 seconds.' >&2
        errors=1
    fi
    export RECORDER_STALE_SECONDS="$value"
    value="${YOUTUBE_SEGMENT_SECONDS:-21600}"
    if [[ ! "$value" =~ ^[1-9][0-9]{0,4}$ ]] || (( value > 42900 )); then
        echo 'ERROR: YOUTUBE_SEGMENT_SECONDS must be an integer between 1 and 42900.' >&2
        errors=1
    fi
    export YOUTUBE_SEGMENT_SECONDS="$value"
    (( errors == 0 ))
}

configure_user_ids() {
    local target_uid="${PUID:-}" target_gid="${PGID:-}"
    if [[ -n "$target_gid" ]]; then
        if [[ ! "$target_gid" =~ ^[0-9]+$ ]] || (( target_gid == 0 )); then
            echo 'ERROR: PGID must be a positive integer.' >&2
            return 1
        fi
        if (( target_gid != $(id -g "$USER_NAME") )); then
            groupmod -o -g "$target_gid" "$USER_NAME" || return 1
        fi
    fi
    if [[ -n "$target_uid" ]]; then
        if [[ ! "$target_uid" =~ ^[0-9]+$ ]] || (( target_uid == 0 )); then
            echo 'ERROR: PUID must be a positive integer.' >&2
            return 1
        fi
        if (( target_uid != $(id -u "$USER_NAME") )); then
            usermod -o -u "$target_uid" "$USER_NAME" || return 1
        fi
    fi
}

prepare_directories() {
    local directory probe
    pipeline_init
    mkdir -p -- "${CONFIG_DIR:-/config}" "$STORAGE_DIR" \
        "${LOG_DIR:-/var/log/streamlink}" "${DBUS_RUN_DIR:-/var/run/dbus}"
    # Queue directories must already exist before assigning ownership.
    chown -R "$USER_NAME:$USER_NAME" "${CONFIG_DIR:-/config}" "$STORAGE_DIR" \
        "${SCRATCH_ROOT:-/etc/streamlink/scratch}" \
        "${LOG_DIR:-/var/log/streamlink}" "${DBUS_RUN_DIR:-/var/run/dbus}"
    for directory in "$WORK_DIR"/{active,normalize,quarantine,upload,encode,publish,notifications,sessions} \
        "${SCRATCH_ROOT:-/etc/streamlink/scratch}/youtube-quota" \
        "${SCRATCH_ROOT:-/etc/streamlink/scratch}/discord-state" "$STORAGE_DIR" "${CONFIG_DIR:-/config}"; do
        probe=$(gosu "$USER_NAME" mktemp "$directory/.write-test.XXXXXXXX") || {
            printf 'ERROR: Directory is not writable by %s: %s\n' "$USER_NAME" "$directory" >&2
            return 1
        }
        rm -- "$probe"
    done
}

prepare_encoding_device() {
    [[ "${ENCODE:-false}" == true ]] || return 0
    build_encoding_args || return 1
    [[ "$ENCODER_ACCEL" == vaapi || "$ENCODER_ACCEL" == qsv ]] || return 0
    local device="${ENCODE_DEVICE:-/dev/dri/renderD128}" device_gid device_group
    if [[ ! -c "$device" ]]; then
        if [[ "${ENCODE_FALLBACK_CPU:-false}" == true ]]; then
            printf 'WARNING: ENCODE_DEVICE unavailable: %s; CPU fallback is enabled.\n' "$device" >&2
            return 0
        fi
        printf 'ERROR: ENCODE_DEVICE is not an accessible DRM device: %s. Pass the render device into Docker.\n' "$device" >&2
        return 1
    fi
    device_gid=$(stat -c %g -- "$device") || return 1
    device_group=$(getent group "$device_gid" | cut -d: -f1) || device_group=''
    if [[ -z "$device_group" ]]; then
        device_group="recorder-gpu-$device_gid"
        groupadd --gid "$device_gid" "$device_group" || return 1
    fi
    # gosu rebuilds supplementary groups; Docker group_add alone is insufficient.
    # Match the passed-through node's group without changing its host permissions.
    usermod --append --groups "$device_group" "$USER_NAME" || return 1
    if ! gosu "$USER_NAME" test -r "$device" || ! gosu "$USER_NAME" test -w "$device"; then
        if [[ "${ENCODE_FALLBACK_CPU:-false}" == true ]]; then
            printf 'WARNING: ENCODE_DEVICE inaccessible: %s; CPU fallback is enabled.\n' "$device" >&2
            return 0
        fi
        printf 'ERROR: %s cannot read/write ENCODE_DEVICE: %s. Check host device permissions.\n' "$USER_NAME" "$device" >&2
        return 1
    fi
}

validate_upload_credentials() {
    [[ "$UPLOAD" == true ]] || return 0
    local filename
    # Pass absolute credential paths to the uploader; no mutable /etc symlinks.
    for filename in youtubeuploader_client_secrets.json youtubeuploader_request.token; do
        if ! gosu "$USER_NAME" test -f "${CONFIG_DIR:-/config}/$filename" || \
            ! gosu "$USER_NAME" test -r "${CONFIG_DIR:-/config}/$filename"; then
            printf 'ERROR: Upload credential is missing or unreadable: %s\n' "$filename" >&2
            return 1
        fi
    done
    if ! gosu "$USER_NAME" test -w "${CONFIG_DIR:-/config}/youtubeuploader_request.token"; then
        printf 'ERROR: Upload token is not writable by %s: youtubeuploader_request.token\n' "$USER_NAME" >&2
        return 1
    fi
}

initialize_dbus() {
    local environment_file session_path
    environment_file=$(mktemp /tmp/streamlink-dbus.XXXXXXXX)
    if ! gosu "$USER_NAME" dbus-launch --sh-syntax > "$environment_file"; then
        rm -- "$environment_file"
        return 1
    fi
    # dbus-launch emits trusted shell assignments for the session just started.
    # shellcheck disable=SC1090
    source "$environment_file"
    rm -- "$environment_file"
    export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID
    session_path="${DBUS_SESSION_BUS_ADDRESS#unix:path=}"
    session_path="${session_path%%,*}"
    if [[ -S "$session_path" && ! -S /run/dbus/system_bus_socket ]]; then
        ln -sfn -- "$session_path" /run/dbus/system_bus_socket
        export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    fi
}

entrypoint_main() {
    validate_environment
    configure_user_ids
    prepare_directories
    prepare_encoding_device
    validate_upload_credentials
    chmod 666 /dev/stdout /dev/stderr
    initialize_dbus
    printf 'Starting application as %s (UID: %s)...\n' "$USER_NAME" "$(id -u "$USER_NAME")"
    exec gosu "$USER_NAME" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    entrypoint_main "$@"
fi
