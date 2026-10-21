#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"
ACCESS_TOKEN=''
TOKEN_EXPIRES_AT=0
CHECK_INTERVAL=${CHECK_INTERVAL:-30}

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
    if ! jq -e '.data | type == "array"' "$body" > /dev/null 2>&1; then
        rm -f -- "$body"
        return 1
    fi
    cat -- "$body"
    rm -f -- "$body"
}

fetch_kick_streamlink_info() {
    local json status=0
    local -a resolver_args=(--webbrowser true --webbrowser-headless true)
    if [[ -n "${STREAMLINK_EXTRA_ARGS:-}" ]]; then
        local -a extra_resolver_args=()
        load_extra_args extra_resolver_args "$STREAMLINK_EXTRA_ARGS" || return 1
        resolver_args+=("${extra_resolver_args[@]}")
    fi
    # Poll once: capture's --retry-streams would otherwise wait forever offline.
    json=$(timeout --signal=INT --kill-after=5 90 streamlink "${resolver_args[@]}" \
        --retry-streams 0 --retry-max 0 --json "https://kick.com/$CHANNEL") || status=$?
    if (( status != 0 )); then
        # Only an explicit no-streams result confirms offline, not resolver errors.
        (( status == 1 )) || return 1
        jq -e --arg message "No playable streams found on this URL: https://kick.com/$CHANNEL" \
            '.error == $message' <<< "$json" > /dev/null 2>&1 || return 1
        printf '{"data":[]}\n'
        return 0
    fi
    jq -e '
        select(.error == null and (.streams | type == "object")) |
        if (.streams | length) == 0 then {data: []}
        else
            .metadata as $m |
            select(($m.title | type == "string") and ($m.author | type == "string") and
                   (($m.id | type) == "string" or ($m.id | type) == "number")) |
            select(($m.id | tostring | length) > 0) |
            {data: [{stream_title: $m.title, slug: $m.author,
                     resolver_id: ($m.id | tostring), stream: {is_live: true}}]}
        end' <<< "$json"
}

build_streamlink_args() {
    STREAMLINK_ARGS=(--retry-streams 30 --retry-open 5 --stream-timeout 60 --loglevel info --webbrowser true --webbrowser-headless true)
    if [[ "$MODE" == twitch && -n "${TWITCH_USER_TOKEN:-}" ]]; then
        STREAMLINK_ARGS+=(--twitch-force-client-integrity --twitch-api-header "Authorization=OAuth $TWITCH_USER_TOKEN")
    fi
    if [[ -n "${STREAMLINK_EXTRA_ARGS:-}" ]]; then
        local -a extra_streamlink_args=()
        load_extra_args extra_streamlink_args "$STREAMLINK_EXTRA_ARGS" || return 1
        STREAMLINK_ARGS+=("${extra_streamlink_args[@]}")
    fi
}

set_channel_status() {
    [[ -n "${WORK_DIR:-}" ]] || return 0
    write_state "$WORK_DIR/channel.status" "$1" &&
        write_state "$WORK_DIR/recorder.heartbeat" "$(date +%s)"
}

record_stream() {
    local title="$1" author="$2" stream_id="$3" url stream_url output quality capture_status=0
    check_free_space "$WORK_DIR/active" || {
        log_failure 'Insufficient disk space in work directory to start recording' "$WORK_DIR/active"
        return 1
    }
    check_free_space "$STORAGE_DIR" || {
        log_failure 'Insufficient disk space in storage directory to start recording' "$STORAGE_DIR"
        return 1
    }
    build_streamlink_args || return 1
    new_recording_job "$title" "$author" "$stream_id" || return 1
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
        log_failure 'Segmented capture failed' "$RECORDING_JOB"
        return 1
    fi
    python3 "$(dirname -- "${BASH_SOURCE[0]}")/capture.py" "$WORK_DIR/active" -- \
        streamlink "${STREAMLINK_ARGS[@]}" --output "$output" "$url" "$quality" || capture_status=$?
    if (( capture_status == 75 )); then
        local available_gb
        available_gb=$(df -k --output=avail "$WORK_DIR/active" | tail -n 1 | tr -d ' ') || available_gb=0
        available_gb=$((available_gb / 1024 / 1024))
        notify_low_disk_space "$WORK_DIR/active" "$available_gb" "${MIN_FREE_SPACE_GB:-0}" || true
    fi
    if [[ ! -s "$output" ]]; then
        log_failure 'No media captured; discarding empty recording' "$RECORDING_JOB"
        discard_empty_recording "$RECORDING_JOB" || log_failure 'Empty recording cleanup pending' "$RECORDING_JOB"
        return 1
    fi
    if (( capture_status != 0 )); then
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
    local json='' live title author stream_id status started
    if refresh_token; then
        if json=$(fetch_stream_info); then
            :
        else
            status=$?
            # Invalidate in the parent shell, not fetch_stream_info's substitution.
            [[ "$status" != 2 ]] || TOKEN_EXPIRES_AT=0
            [[ "$MODE" == kick ]] || return 1
            json=$(fetch_kick_streamlink_info) || return 1
        fi
    else
        [[ "$MODE" == kick ]] || return 1
        json=$(fetch_kick_streamlink_info) || return 1
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
        if ! stream_id=$(jq -er '.data[0].resolver_id' <<< "$json"); then
            started=$(jq -er '.data[0].stream.start_time' <<< "$json") &&
                stream_id=$(date -d "$started" +%s) || return 1
        fi
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
