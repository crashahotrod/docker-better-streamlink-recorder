#!/bin/bash
# Queue one past Twitch VOD using the same verified processing route as live captures.
set -euo pipefail
# shellcheck source=download.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/download.sh"

capture_vod() {
    local capture_pid capture_status=0 saved_term saved_int
    saved_term=$(trap -p TERM)
    saved_int=$(trap -p INT)
    ARCHIVE_CHAT=false STREAM_CHAPTERS=false python3 "$(dirname -- "${BASH_SOURCE[0]}")/capture.py" "$@" <&0 &
    capture_pid=$!
    trap 'kill -TERM "$capture_pid" 2>/dev/null || true' TERM INT
    while true; do
        if wait "$capture_pid"; then capture_status=0; else capture_status=$?; fi
        kill -0 "$capture_pid" 2>/dev/null || break
    done
    trap - TERM INT
    if [[ -n "$saved_term" ]]; then eval "$saved_term"; fi
    if [[ -n "$saved_int" ]]; then eval "$saved_int"; fi
    return "$capture_status"
}

catchup_main() {
    local vod="${1:-}" json title author created recorded_at job_state previous_job=''
    [[ "$vod" =~ ^[1-9][0-9]*$ ]] || { echo 'Usage: catchup_vod.sh TWITCH_VOD_ID' >&2; return 1; }
    [[ "${MODE:-twitch}" == twitch ]] || { echo 'VOD catch-up currently supports Twitch only.' >&2; return 1; }
    export MODE=twitch
    : "${CHANNEL:?Set CHANNEL to the VOD owner}" "${CLIENT_ID:?Set CLIENT_ID}" "${CLIENT_SECRET:?Set CLIENT_SECRET}"
    [[ "$CHANNEL" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_-]*$ ]] || { echo 'CHANNEL must be a channel slug.' >&2; return 1; }
    pipeline_init || return 1
    mkdir -p "$WORK_DIR/vods"
    exec 8> "$WORK_DIR/vods/$vod.lock"
    flock -n 8 || { echo 'This VOD already has a catch-up writer.' >&2; return 1; }
    job_state="$WORK_DIR/vods/$vod.state"
    if [[ -f "$job_state" ]]; then
        read -r previous_job < "$WORK_DIR/vods/$vod.job" || true
        if [[ $(cat "$job_state") == queued ]]; then
            printf 'VOD %s was already queued (%s).\n' "$vod" "$previous_job"
            return 0
        fi
        echo 'An earlier catch-up attempt is retained; inspect/recover its job before retrying.' >&2
        return 1
    fi
    refresh_token || return 1
    json=$(curl -fsS --connect-timeout 15 --max-time 60 \
        -H "Authorization: Bearer $ACCESS_TOKEN" -H "Client-ID: $CLIENT_ID" \
        "https://api.twitch.tv/helix/videos?id=$vod") || return 1
    author=$(jq -er --arg id "$vod" '.data[] | select(.id == $id) | .user_login' <<< "$json") &&
        title=$(jq -er '.data[0].title' <<< "$json") &&
        created=$(jq -er '.data[0].created_at' <<< "$json") || return 1
    [[ "${author,,}" == "${CHANNEL,,}" ]] || { echo 'VOD owner does not match CHANNEL.' >&2; return 1; }
    recorded_at=$(date -d "$created" +%s) || return 1
    check_free_space "$WORK_DIR/active" && check_free_space "$STORAGE_DIR" || return 1
    build_streamlink_args || return 1
    REGISTER_ACTIVE_RECORDING=false YOUTUBE_SPLITTING=false \
        new_recording_job "$title" "$author" "vod-$vod" "$recorded_at" || return 1
    write_state "$RECORDING_JOB/vod-id" "$vod"
    write_state "$WORK_DIR/vods/$vod.job" "${RECORDING_JOB##*/}"
    write_state "$job_state" capturing
    # Live chat and live metadata do not describe an archived VOD.
    if ! capture_vod "$RECORDING_JOB" -- streamlink "${STREAMLINK_ARGS[@]}" --retry-streams 0 --retry-max 0 \
        --output "$RECORDING_JOB/capture.bin" "https://www.twitch.tv/videos/$vod" "${STREAM_QUALITY:-best}"; then
        log_failure 'VOD capture interrupted; original retained for offline recovery' "$RECORDING_JOB"
        return 1
    fi
    complete_recording "$RECORDING_JOB" || return 1
    write_state "$job_state" queued
    printf 'Queued VOD %s: %s\n' "$vod" "${RECORDING_JOB##*/}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    catchup_main "$@"
fi
