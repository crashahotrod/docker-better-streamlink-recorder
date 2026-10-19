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
    for name in UPLOAD ENCODE REMUX YOUTUBE_SPLITTING AUTO_RECOVER_INTERRUPTED; do
        value="${!name:-false}"
        case "$value" in true|false) ;; *) printf 'ERROR: %s must be true or false.\n' "$name" >&2; errors=1 ;; esac
        export "$name=$value"
    done
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
    (( errors == 0 ))
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
    for directory in "$WORK_DIR"/{active,normalize,quarantine,upload,encode,publish} "$STORAGE_DIR" "${CONFIG_DIR:-/config}"; do
        probe=$(gosu "$USER_NAME" mktemp "$directory/.write-test.XXXXXXXX") || {
            printf 'ERROR: Directory is not writable by %s: %s\n' "$USER_NAME" "$directory" >&2
            return 1
        }
        rm -- "$probe"
    done
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
    prepare_directories
    validate_upload_credentials
    chmod 666 /dev/stdout /dev/stderr
    initialize_dbus
    printf 'Starting application as %s (UID: %s)...\n' "$USER_NAME" "$(id -u "$USER_NAME")"
    exec gosu "$USER_NAME" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    entrypoint_main "$@"
fi
