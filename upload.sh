#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

invoke_uploader() {
    timeout --signal=TERM --kill-after=30s 6h "${YOUTUBEUPLOADER_BIN:-/etc/youtubeuploader}" "$@"
}

upload_job() {
    local job="$1" id input title recorded_date encode count part_title
    local uploader_output video_id video_url notify_msg now reset_time wait_hours
    ensure_normalized_job "$job" || return 1
    read_field "$job/title" title && read -r recorded_date < "$job/recorded-date" || return 1
    count=$(jq '.parts | length' "$job/normalized.json") || return 1
    while IFS= read -r id; do
        input=$(part_file "$job" "$id") || return 1
        if [[ -f "$job/parts/$id.uploaded" ]]; then continue; fi
        part_title="$recorded_date - $title"
        if (( count > 1 )); then part_title="$part_title - part $id"; fi
        local -a uploader_args=(
            -title "$part_title"
            -privacy "${YOUTUBE_PRIVACY:-public}"
            -filename "$input"
            -description "${YOUTUBE_DESCRIPTION:-Uploaded Automatically by ${UPLOAD_BOT_NAME:?}}"
            -secrets "${CONFIG_DIR:-/config}/youtubeuploader_client_secrets.json"
            -cache "${CONFIG_DIR:-/config}/youtubeuploader_request.token"
            -metaJSONout "$job/parts/$id.remote.json"
        )
        if [[ -n "${YOUTUBE_PLAYLIST_ID:-}" ]]; then
            uploader_args+=(-playlistID "$YOUTUBE_PLAYLIST_ID")
        fi
        if [[ -n "${YOUTUBE_TAGS:-}" ]]; then
            uploader_args+=(-tags "$YOUTUBE_TAGS")
        fi
        if [[ -n "${YOUTUBE_CATEGORY_ID:-}" ]]; then
            uploader_args+=(-categoryId "$YOUTUBE_CATEGORY_ID")
        fi
        uploader_output=$(mktemp) || return 1
        if ! invoke_uploader "${uploader_args[@]}" > "$uploader_output" 2>&1; then
            cat "$uploader_output" >&2
            if grep -q 'quotaExceeded' "$uploader_output"; then
                rm -f "$uploader_output"
                now=$(date +%s)
                reset_time=$(date -u -d "08:00" +%s 2>/dev/null || echo 0)
                if [[ "$reset_time" =~ ^[0-9]+$ ]] && (( reset_time > 0 )); then
                    if (( reset_time <= now )); then
                        reset_time=$((reset_time + 86400))
                    fi
                    write_state "$job/upload.retry-at" "$reset_time"
                    wait_hours=$(( (reset_time - now) / 3600 ))
                    printf '[Upload] YouTube API quota exceeded; backing off until 08:00 UTC (~%dh)\n' "$wait_hours" >&2
                    notify_youtube_quota_exceeded "$wait_hours" "$reset_time" || true
                fi
            else
                rm -f "$uploader_output"
            fi
            log_failure 'Upload failed; capture and per-part receipts retained' "$job"
            return 1
        fi
        cat "$uploader_output"
        rm -f "$uploader_output"
        video_id=''
        video_url=''
        if [[ -f "$job/parts/$id.remote.json" ]]; then
            video_id=$(jq -r '.id // empty' "$job/parts/$id.remote.json" 2>/dev/null || true)
            if [[ -n "$video_id" ]]; then
                video_url="https://youtu.be/$video_id"
                printf '[Upload] Video published: %s\n' "$video_url"
            fi
        fi
        if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
            notify_msg="**Upload Complete**: $part_title"
            if [[ -n "$video_url" ]]; then
                notify_msg="$notify_msg"$'\n'"$video_url"
                notify_discord "$notify_msg" "$part_title" "$video_url" "${UPLOAD_BOT_NAME:-YouTube}" || true
            else
                notify_discord "$notify_msg" || true
            fi
        fi
        write_state "$job/parts/$id.uploaded" true || return 1
        printf '[Upload] Part %s completed: %q\n' "$id" "$job"
    done < <(part_ids "$job")
    read -r encode < "$job/encode-required" || return 1
    if [[ "$encode" == true ]]; then advance_job "$job" encode; else advance_job "$job" publish; fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    run_worker upload upload_job
fi
