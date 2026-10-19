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

notify_discord() {
    local message="$1" payload
    payload=$(jq -n --arg content "$message" '{content: $content}') || return 1
    if ! curl -fsS --connect-timeout 10 --max-time 30 -X POST \
        -H 'Content-Type: application/json' --data "$payload" \
        "$DISCORD_WEBHOOK_URL" > /dev/null 2>&1; then
        echo '[Discord] Notification failed; recording will continue.' >&2
        return 1
    fi
}

build_streamlink_args() {
    STREAMLINK_ARGS=(--retry-streams 30 --loglevel info --webbrowser true --webbrowser-headless true)
    if [[ "$MODE" == twitch && -n "${TWITCH_USER_TOKEN:-}" ]]; then
        STREAMLINK_ARGS+=(--twitch-force-client-integrity --twitch-api-header "Authorization=OAuth $TWITCH_USER_TOKEN")
    fi
    if [[ "${YOUTUBE_SPLITTING:-false}" == true ]]; then
        STREAMLINK_ARGS+=(--hls-duration 11h59m30s)
    fi
}

record_stream() {
    local title="$1" author="$2" stream_id="$3" url output
    new_recording_job "$title" "$author" "$stream_id" || return 1
    build_streamlink_args
    if [[ "$MODE" == twitch ]]; then url="twitch.tv/$CHANNEL"; else url="kick.com/$CHANNEL"; fi
    if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
        notify_discord "**$CHANNEL is LIVE!**"$'\n'"Title: $title" || true
    fi
    if [[ "${REMUX:-false}" == true ]]; then
        output="$RECORDING_JOB/media.mp4"
        if ! streamlink "${STREAMLINK_ARGS[@]}" --stdout "$url" best | \
            ffmpeg -nostdin -n -v error -i pipe:0 -c copy -f mp4 "$output"; then
            log_failure 'Recording interrupted; active files retained' "$RECORDING_JOB"
            return 1
        fi
    else
        output="$RECORDING_JOB/media.ts"
        if ! streamlink "${STREAMLINK_ARGS[@]}" --output "$output" "$url" best; then
            log_failure 'Recording interrupted; active files retained' "$RECORDING_JOB"
            return 1
        fi
    fi
    complete_recording "$RECORDING_JOB" || {
        log_failure 'Completion pending; files retained' "$RECORDING_JOB"
        return 1
    }
}

monitor_once() {
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
    if [[ "$MODE" == twitch ]]; then
        live=$(jq -er '.data | length' <<< "$json") || return 1
        [[ "$live" != 0 ]] || return 0
        title=$(jq -er '.data[0].title' <<< "$json") &&
            author=$(jq -er '.data[0].user_name' <<< "$json") &&
            stream_id=$(jq -er '.data[0].id' <<< "$json") || return 1
    else
        live=$(jq -r '.data[0].stream.is_live // false' <<< "$json") || return 1
        [[ "$live" == true ]] || return 0
        title=$(jq -er '.data[0].stream_title' <<< "$json") &&
            author=$(jq -er '.data[0].slug' <<< "$json") || return 1
        started=$(jq -er '.data[0].stream.start_time' <<< "$json") &&
            stream_id=$(date -d "$started" +%s) || return 1
    fi
    record_stream "$title" "$author" "$stream_id"
}

download_main() {
    pipeline_init || return 1
    # One monitor owns active recordings per channel.
    exec 9> "$WORK_DIR/download.lock"
    flock -n 9 || return 1
    while true; do
        recover_completed_recordings
        monitor_once || printf '[Monitor] Check or recording failed; retrying in %ss.\n' "$CHECK_INTERVAL" >&2
        sleep "$CHECK_INTERVAL"
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    download_main
fi
