#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"
ACCESS_TOKEN=''
TOKEN_EXPIRES_AT=0
CHECK_INTERVAL=30

refresh_token() {
    local endpoint response expires now
    now=$(date +%s)
    (( now < TOKEN_EXPIRES_AT )) && return 0
    case "$MODE" in
        twitch) endpoint=https://id.twitch.tv/oauth2/token ;;
        kick) endpoint=https://id.kick.com/oauth/token ;;
        *) return 1 ;;
    esac
    response=$(curl -fsS --connect-timeout 15 --max-time 60 -X POST "$endpoint" \
        --data-urlencode "client_id=$CLIENT_ID" \
        --data-urlencode "client_secret=$CLIENT_SECRET" \
        -d grant_type=client_credentials 2>/dev/null) || return 1
    ACCESS_TOKEN=$(jq -er '.access_token | select(type == "string" and length > 0)' <<< "$response") || return 1
    expires=$(jq -er '.expires_in | select(type == "number" and . > 0)' <<< "$response") || return 1
    [[ "$expires" =~ ^[0-9]+$ ]] || return 1
    TOKEN_EXPIRES_AT=$((now + (expires > 60 ? expires - 60 : 1)))
}

fetch_stream_info() {
    local endpoint body status
    local -a headers=(-H "Authorization: Bearer $ACCESS_TOKEN")
    if [[ "$MODE" == twitch ]]; then
        endpoint="https://api.twitch.tv/helix/streams?user_login=$CHANNEL"
        headers+=(-H "Client-ID: $CLIENT_ID")
    else
        endpoint="https://api.kick.com/public/v1/channels?slug=$CHANNEL"
    fi
    body=$(mktemp) || return 1
    status=$(curl -s --connect-timeout 15 --max-time 60 -o "$body" -w '%{http_code}' \
        "${headers[@]}" "$endpoint") || { rm -f -- "$body"; return 1; }
    if [[ "$status" != 200 ]]; then
        rm -f -- "$body"
        [[ "$status" != 401 ]] || return 2
        return 1
    fi
    cat -- "$body"
    rm -f -- "$body"
}

build_streamlink_args() {
    STREAMLINK_ARGS=(--retry-streams 30 --loglevel info --webbrowser true --webbrowser-headless true)
    if [[ "$MODE" == twitch && -n "${TWITCH_USER_TOKEN:-}" ]]; then
        STREAMLINK_ARGS+=(--twitch-force-client-integrity --twitch-api-header "Authorization=OAuth $TWITCH_USER_TOKEN")
    fi
    if [[ -n "${STREAMLINK_EXTRA_ARGS:-}" ]]; then
        local -a extra_streamlink_args=()
        read -r -a extra_streamlink_args <<< "$STREAMLINK_EXTRA_ARGS"
        STREAMLINK_ARGS+=("${extra_streamlink_args[@]}")
    fi
}

set_channel_status() {
    [[ -n "${WORK_DIR:-}" ]] || return 0
    write_state "$WORK_DIR/channel.status" "$1" &&
        write_state "$WORK_DIR/recorder.heartbeat" "$(date +%s)"
}

record_stream() {
    local title="$1" author="$2" stream_id="$3" url stream_url output quality
    check_free_space "$WORK_DIR/active" || {
        log_failure 'Insufficient disk space in work directory to start recording' "$WORK_DIR/active"
        return 1
    }
    check_free_space "$STORAGE_DIR" || {
        log_failure 'Insufficient disk space in storage directory to start recording' "$STORAGE_DIR"
        return 1
    }
    new_recording_job "$title" "$author" "$stream_id" || return 1
    build_streamlink_args
    if [[ "$MODE" == twitch ]]; then url="twitch.tv/$CHANNEL"; else url="kick.com/$CHANNEL"; fi
    stream_url="https://$url"
    if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
        notify_discord "**$CHANNEL is LIVE!**"$'\n'"Title: $title" "$title" "$stream_url" "$author" || true
    fi
    # Keep the original independent of normalization failures and decoder exits.
    output="$RECORDING_JOB/capture.bin"
    quality="${STREAM_QUALITY:-best}"
    if [[ "${YOUTUBE_SPLITTING:-false}" == true ]]; then
        if python3 "$(dirname -- "${BASH_SOURCE[0]}")/segment_capture.py" capture "$RECORDING_JOB" -- \
            streamlink "${STREAMLINK_ARGS[@]}" --stdout "$url" "$quality"; then
            notify_recording_completed "$WORK_DIR/sessions/${RECORDING_JOB##*/}" || true
            write_state "$WORK_DIR/sessions/${RECORDING_JOB##*/}/broadcast-finished" true || \
                log_failure 'Broadcast completion acknowledgment pending' "$RECORDING_JOB"
            return 0
        fi
        log_failure 'Segmented capture interrupted; session and unfinished children retained' "$RECORDING_JOB"
        return 1
    fi
    if ! streamlink "${STREAMLINK_ARGS[@]}" --output "$output" "$url" "$quality"; then
        log_failure 'Recording interrupted; active capture retained' "$RECORDING_JOB"
        if [[ "${AUTO_RECOVER_INTERRUPTED:-false}" == true && -s "$output" ]]; then
            printf '[Pipeline] Auto-recovering interrupted capture: %q\n' "$RECORDING_JOB" >&2
            notify_recording_completed "$RECORDING_JOB" || true
            complete_recording "$RECORDING_JOB" || log_failure 'Interrupted capture recovery pending' "$RECORDING_JOB"
        fi
        [[ -z "${WORK_DIR:-}" ]] || rm -f -- "$WORK_DIR/active.job"
        return 1
    fi
    notify_recording_completed "$RECORDING_JOB" || true
    complete_recording "$RECORDING_JOB" || {
        log_failure 'Completion pending; files retained' "$RECORDING_JOB"
        [[ -z "${WORK_DIR:-}" ]] || rm -f -- "$WORK_DIR/active.job"
        return 1
    }
}

check_stream() {
    local json live title author stream_id status started
    refresh_token || return 1
    if json=$(fetch_stream_info); then
        :
    else
        status=$?
        # Invalidate in the parent shell, not fetch_stream_info's substitution.
        [[ "$status" != 2 ]] || TOKEN_EXPIRES_AT=0
        return 1
    fi
    jq -e '.data | type == "array"' <<< "$json" > /dev/null || return 1
    if [[ "$MODE" == twitch ]]; then
        live=$(jq -er '.data | length' <<< "$json") || return 1
        if [[ "$live" == 0 ]]; then
            set_channel_status "offline"
            return 0
        fi
        title=$(jq -er '.data[0].title' <<< "$json") &&
            author=$(jq -er '.data[0].user_name' <<< "$json") &&
            stream_id=$(jq -er '.data[0].id' <<< "$json") || return 1
    else
        live=$(jq -r 'if (.data | length) == 0 then false else .data[0].stream.is_live end | select(type == "boolean")' <<< "$json") || return 1
        [[ "$live" == true || "$live" == false ]] || return 1
        if [[ "$live" != true ]]; then
            set_channel_status "offline"
            return 0
        fi
        title=$(jq -er '.data[0].stream_title' <<< "$json") &&
            author=$(jq -er '.data[0].slug' <<< "$json") || return 1
        started=$(jq -er '.data[0].stream.start_time' <<< "$json") &&
            stream_id=$(date -d "$started" +%s) || return 1
    fi
    set_channel_status "recording"
    record_stream "$title" "$author" "$stream_id" || return 1
    set_channel_status "monitoring"
}

monitor_once() {
    set_channel_status "monitoring"
    if check_stream; then return 0; fi
    set_channel_status "error"
    return 1
}

download_main() {
    pipeline_init || return 1
    # One monitor owns active recordings per channel.
    exec 9> "$WORK_DIR/download.lock"
    flock -n 9 || return 1
    # Old jobs remain available for recovery, but cannot describe this monitor.
    rm -f -- "$WORK_DIR/active.job"
    set_channel_status "monitoring"
    while true; do
        recover_completed_recordings
        monitor_once || printf '[Monitor] Check or recording failed; retrying in %ss.\n' "$CHECK_INTERVAL" >&2
        sleep "$CHECK_INTERVAL"
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    download_main
fi
