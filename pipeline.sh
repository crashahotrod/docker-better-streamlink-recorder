#!/bin/bash
# Shared queue and publication operations. Callers handle failures explicitly.

pipeline_init() {
    WORK_DIR="${SCRATCH_ROOT:-/etc/streamlink/scratch}/${MODE:?}/${CHANNEL:?}/queue-v2"
    STORAGE_DIR="${STORAGE_DIR:-/storage}"
    mkdir -p -- "$WORK_DIR"/{active,normalize,quarantine,upload,encode,publish} || return 1
}

log_failure() {
    printf '[Pipeline] %s: %q\n' "$1" "$2" >&2
}

read_field() {
    IFS= read -r -d '' "$2" < "$1"
}

write_state() {
    printf '%s\n' "$2" > "$1.tmp" && mv -f -- "$1.tmp" "$1"
}

check_free_space() {
    local target="$1" min_gb="${MIN_FREE_SPACE_GB:-0}"
    [[ "$min_gb" =~ ^[0-9]+$ ]] || min_gb=0
    (( min_gb > 0 )) || return 0
    local available_kb
    available_kb=$(df -k --output=avail "$target" 2>/dev/null | tail -n 1 | tr -d ' ') || return 0
    [[ "$available_kb" =~ ^[0-9]+$ ]] || return 0
    local available_gb=$((available_kb / 1024 / 1024))
    if (( available_gb < min_gb )); then
        printf '[Pipeline] Disk space low on %s: %sGB available (minimum %sGB required)\n' \
            "$target" "$available_gb" "$min_gb" >&2
        notify_low_disk_space "$target" "$available_gb" "$min_gb" || true
        return 1
    fi
    return 0
}

format_duration() {
    local total_seconds="$1"
    [[ "$total_seconds" =~ ^[0-9]+$ ]] || total_seconds=0
    local hours=$((total_seconds / 3600))
    local minutes=$(((total_seconds % 3600) / 60))
    local seconds=$((total_seconds % 60))
    printf '%02d:%02d:%02d\n' "$hours" "$minutes" "$seconds"
}

format_bytes() {
    local bytes="$1"
    [[ "$bytes" =~ ^[0-9]+$ ]] || bytes=0
    if (( bytes < 1024 )); then
        printf '%d B\n' "$bytes"
    elif (( bytes < 1048576 )); then
        awk -v b="$bytes" 'BEGIN { printf "%.2f KB\n", b / 1024 }'
    elif (( bytes < 1073741824 )); then
        awk -v b="$bytes" 'BEGIN { printf "%.2f MB\n", b / 1048576 }'
    else
        awk -v b="$bytes" 'BEGIN { printf "%.2f GB\n", b / 1073741824 }'
    fi
}

get_monitor_webhook() {
    if [[ -n "${DISCORD_MONITOR_WEBHOOK_URL:-}" ]]; then
        printf '%s\n' "$DISCORD_MONITOR_WEBHOOK_URL"
    elif [[ "${DISCORD_NOTIFY_ON_ERROR:-true}" == true && -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
        printf '%s\n' "$DISCORD_WEBHOOK_URL"
    fi
}

notify_monitor_alert() {
    local title="$1" message="$2" color="${3:-15105570}" fields_json="${4:-[]}"
    if [[ "${DISCORD_NOTIFY_ON_ERROR:-true}" == false ]]; then
        return 0
    fi
    local webhook
    webhook=$(get_monitor_webhook)
    [[ -n "$webhook" ]] || return 0
    webhook="${webhook%/}"

    local payload
    payload=$(jq -n \
        --arg title "$title" \
        --arg desc "$message" \
        --arg author "${CHANNEL:-Streamlink Recorder}" \
        --argjson color "$color" \
        --argjson fields "$fields_json" \
        '{
            embeds: [{
                title: $title,
                description: $desc,
                color: $color,
                author: { name: $author },
                fields: $fields,
                timestamp: (now | todate),
                footer: { text: "Better Streamlink Recorder" }
            }]
        }') || return 1

    if ! curl -fsS --connect-timeout 10 --max-time 30 -X POST \
        -H 'Content-Type: application/json' --data "$payload" \
        "$webhook" > /dev/null 2>&1; then
        echo '[Discord Alert] Notification failed; continuing operations.' >&2
        return 1
    fi
    return 0
}

notify_recording_completed() {
    local job="$1" title="Untitled Stream" started_at now duration_sec duration_str capture_file size_bytes=0 size_str parts=1 fields
    if [[ -f "$job/title" ]]; then
        read_field "$job/title" title || title="Untitled Stream"
    fi
    now=$(date +%s)
    started_at=$now
    if [[ -f "$job/started-at" ]]; then
        read -r started_at < "$job/started-at" || started_at=$now
    fi
    [[ "$started_at" =~ ^[0-9]+$ ]] || started_at=$now
    duration_sec=$(( now - started_at ))
    (( duration_sec >= 0 )) || duration_sec=0
    duration_str=$(format_duration "$duration_sec")

    capture_file="$job/capture.bin"
    if [[ -f "$capture_file" ]]; then
        size_bytes=$(stat -c %s "$capture_file" 2>/dev/null || stat -f %z "$capture_file" 2>/dev/null || wc -c < "$capture_file" 2>/dev/null || echo 0)
    elif [[ -f "$job/media.mp4" ]]; then
        size_bytes=$(stat -c %s "$job/media.mp4" 2>/dev/null || stat -f %z "$job/media.mp4" 2>/dev/null || wc -c < "$job/media.mp4" 2>/dev/null || echo 0)
    elif [[ -f "$job/media.ts" ]]; then
        size_bytes=$(stat -c %s "$job/media.ts" 2>/dev/null || stat -f %z "$job/media.ts" 2>/dev/null || wc -c < "$job/media.ts" 2>/dev/null || echo 0)
    fi
    [[ "$size_bytes" =~ ^[0-9]+$ ]] || size_bytes=0
    size_str=$(format_bytes "$size_bytes")

    if [[ -f "$job/normalized.json" ]]; then
        parts=$(jq '.parts | length' "$job/normalized.json" 2>/dev/null || echo 1)
    fi

    fields=$(jq -n \
        --arg title "$title" \
        --arg duration "$duration_str" \
        --arg size "$size_str" \
        --argjson parts "$parts" \
        '[
            {name: "Stream Title", value: $title, inline: false},
            {name: "Duration", value: $duration, inline: true},
            {name: "Size", value: $size, inline: true},
            {name: "Parts", value: ($parts | tostring), inline: true}
        ]')

    notify_monitor_alert "🎬 Recording Completed: ${CHANNEL:-stream}" \
        "Stream recording has completed and is queued for processing." \
        3066993 "$fields"
}

notify_low_disk_space() {
    local target="$1" available_gb="$2" min_gb="$3" now last_alert=0
    now=$(date +%s)
    if [[ -f "$WORK_DIR/.last_disk_alert" ]]; then
        read -r last_alert < "$WORK_DIR/.last_disk_alert" || last_alert=0
    fi
    [[ "$last_alert" =~ ^[0-9]+$ ]] || last_alert=0
    if (( now - last_alert < 300 )); then
        return 0
    fi
    write_state "$WORK_DIR/.last_disk_alert" "$now"

    local fields
    fields=$(jq -n \
        --arg target "$target" \
        --arg avail "${available_gb} GB" \
        --arg min "${min_gb} GB" \
        '[
            {name: "Path", value: $target, inline: false},
            {name: "Available Space", value: $avail, inline: true},
            {name: "Required Minimum", value: $min, inline: true}
        ]')

    notify_monitor_alert "⚠️ Low Disk Space Warning" \
        "Free disk space has dropped below the minimum threshold on ${CHANNEL:-system}." \
        15105570 "$fields"
}

notify_job_quarantined() {
    local job="$1" title="Unknown Stream" job_id="${1##*/}" fields
    if [[ -f "$job/title" ]]; then
        read_field "$job/title" title || title="Unknown Stream"
    fi
    fields=$(jq -n \
        --arg id "$job_id" \
        --arg channel "${CHANNEL:-unknown}" \
        --arg title "$title" \
        '[
            {name: "Job ID", value: $id, inline: true},
            {name: "Channel", value: $channel, inline: true},
            {name: "Stream Title", value: $title, inline: false},
            {name: "Resolution Required", value: "Media normalization or verification failed. Capture is retained in quarantine for administrator recovery.", inline: false}
        ]')

    notify_monitor_alert "🚨 Job Quarantined: $job_id" \
        "Corrupted capture requires manual inspection." \
        15158332 "$fields"
}

notify_youtube_quota_exceeded() {
    local wait_hours="$1" reset_time="$2" fields
    fields=$(jq -n \
        --arg channel "${CHANNEL:-unknown}" \
        --arg wait "~${wait_hours} hours" \
        --arg reset "08:00 UTC" \
        --argjson reset_epoch "$reset_time" \
        '[
            {name: "Channel", value: $channel, inline: true},
            {name: "Backoff Duration", value: $wait, inline: true},
            {name: "Uploads Resume", value: $reset, inline: true}
        ]')

    notify_monitor_alert "🛑 YouTube API Quota Exceeded" \
        "Daily upload limit reached. YouTube uploads are paused until quota reset at 08:00 UTC." \
        10181046 "$fields"
}

notify_discord() {
    local message="$1" title="${2:-}" url="${3:-}" author="${4:-}"
    [[ -n "${DISCORD_WEBHOOK_URL:-}" ]] || return 0
    local payload
    if [[ -n "$title" && -n "$url" ]]; then
        local color=9520895
        if [[ "${MODE:-}" == kick ]]; then color=5504024; fi
        payload=$(jq -n --arg content "$message" \
            --arg title "$title" \
            --arg url "$url" \
            --arg author "${author:-$CHANNEL}" \
            --argjson color "$color" \
            '{
                content: $content,
                embeds: [{
                    title: $title,
                    url: $url,
                    color: $color,
                    author: { name: $author, url: $url },
                    timestamp: (now | todate)
                }]
            }') || payload=$(jq -n --arg content "$message" '{content: $content}')
    else
        payload=$(jq -n --arg content "$message" '{content: $content}') || return 1
    fi
    if ! curl -fsS --connect-timeout 10 --max-time 30 -X POST \
        -H 'Content-Type: application/json' --data "$payload" \
        "$DISCORD_WEBHOOK_URL" > /dev/null 2>&1; then
        echo '[Discord] Notification failed; recording will continue.' >&2
        return 1
    fi
}

prune_old_recordings() {
    local retention_days="${RETENTION_DAYS:-0}"
    [[ "$retention_days" =~ ^[0-9]+$ && "$retention_days" -gt 0 ]] || return 0
    local channel_storage="$STORAGE_DIR/$CHANNEL"
    [[ -d "$channel_storage" ]] || return 0
    find "$channel_storage" -type f \( -name '*.mp4' -o -name '*.ts' -o -name '*.mkv' \) -mtime "+$retention_days" -delete 2>/dev/null || true
    find "$channel_storage" -mindepth 1 -type d -empty -delete 2>/dev/null || true
}

run_post_publish() {
    local file="$1"
    if [[ -n "${POST_PUBLISH_COMMAND:-}" ]]; then
        (
            export PUBLISHED_FILE="$file"
            export PUBLISHED_CHANNEL="$CHANNEL"
            export PUBLISHED_MODE="$MODE"
            eval "$POST_PUBLISH_COMMAND"
        ) || log_failure 'Post-publish command failed' "$file"
    fi
}

MEDIA_TOOL="$(dirname -- "${BASH_SOURCE[0]}")/media.py"

media_command() {
    python3 "$MEDIA_TOOL" "$@"
}

quarantine_job() {
    local job="$1"
    log_failure 'Media requires recovery; capture retained' "$job"
    if [[ "${job%/*}" != "$WORK_DIR/quarantine" ]]; then
        advance_job "$job" quarantine || return 1
    fi
    notify_job_quarantined "$WORK_DIR/quarantine/${job##*/}" || true
    return 1
}

ensure_normalized_job() {
    local job="$1" status
    if [[ -f "$job/normalized.json" ]]; then
        if media_command check "$job"; then return 0; else status=$?; fi
    else
        if media_command normalize "$job"; then return 0; else status=$?; fi
    fi
    if [[ "$status" -eq 2 ]]; then quarantine_job "$job"; fi
    return 1
}

verify_part() {
    local job="$1" status
    if media_command verify "$@"; then return 0; else status=$?; fi
    if [[ "$status" -eq 2 ]]; then quarantine_job "$job"; fi
    return 1
}

part_ids() {
    jq -r '.parts[].id' "$1/normalized.json"
}

part_file() {
    local relative
    relative=$(jq -er --arg id "$2" '.parts[] | select(.id == $id) | .file' "$1/normalized.json") || return 1
    [[ "$relative" =~ ^parts/[0-9]{4}\.(mp4|ts)$ ]] || return 1
    printf '%s/%s\n' "$1" "$relative"
}

part_destination() {
    local job="$1" id="$2" extension="$3" stem season count
    read_field "$job/stem" stem && read -r season < "$job/season" || return 1
    [[ "$season" =~ ^[0-9]{6}$ && "$stem" != */* && -n "$stem" && "$id" =~ ^[0-9]{4}$ ]] || return 1
    [[ "$extension" == mp4 || "$extension" == ts ]] || return 1
    count=$(jq '.parts | length' "$job/normalized.json") || return 1
    if (( count > 1 )); then stem="$stem - part-$id"; fi
    printf '%s/%s/Season %s/%s.%s\n' "$STORAGE_DIR" "$CHANNEL" "$season" "$stem" "$extension"
}

file_hash() {
    local value
    value=$(sha256sum < "$1") || return 1
    printf '%s\n' "${value%% *}"
}

part_committed() {
    local final checksum
    final=$(part_destination "$1" "$2" "$3") || return 1
    [[ -f "$final" && -f "$1/parts/$2.published.sha256" ]] || return 1
    read -r checksum < "$1/parts/$2.published.sha256" || return 1
    [[ "$checksum" == "$(file_hash "$final")" ]]
}

publish_part() (
    local job="$1" id="$2" input="$3" extension="${3##*.}" final temporary='' title checksum
    final=$(part_destination "$job" "$id" "$extension") || return 1
    if [[ -e "$final" ]]; then part_committed "$job" "$id" "$extension"; return; fi
    read_field "$job/title" title || return 1
    mkdir -p -- "${final%/*}" || return 1
    temporary=$(mktemp -d "${final%/*}/.publish.XXXXXXXX") || return 1
    trap 'rm -f -- "$temporary/media.$extension"; rmdir -- "$temporary"' EXIT
    cp -- "$input" "$temporary/media.$extension" && cmp -s -- "$input" "$temporary/media.$extension" || return 1
    if [[ "$extension" == mp4 ]]; then
        exiftool -q -q -title="$title" -api largefilesupport=1 -overwrite_original "$temporary/media.$extension" || return 1
    fi
    verify_part "$job" "$id" "$temporary/media.$extension" --publication || return 1
    checksum=$(file_hash "$temporary/media.$extension") || return 1
    write_state "$job/parts/$id.published.extension" "$extension" &&
        write_state "$job/parts/$id.published.sha256" "$checksum" || return 1
    ln -- "$temporary/media.$extension" "$final" || return 1
    run_post_publish "$final"
)

job_media() {
    if [[ -f "$1/media.mp4" ]]; then
        printf '%s\n' "$1/media.mp4"
    elif [[ -f "$1/media.ts" ]]; then
        printf '%s\n' "$1/media.ts"
    else
        return 1
    fi
}

remove_completed_job() {
    local job="$1" retired id extension
    [[ "$job" == "$WORK_DIR/"* && "$job" != "$WORK_DIR/" && -f "$job/normalized.json" ]] || return 1
    media_command check "$job" || return 1
    while IFS= read -r id; do
        read -r extension < "$job/parts/$id.published.extension" || return 1
        part_committed "$job" "$id" "$extension" || return 1
    done < <(part_ids "$job")
    retired="${job%/*}/.completed.${job##*/}"
    [[ ! -e "$retired" ]] || return 1
    mv -T -n -- "$job" "$retired" && [[ ! -d "$job" ]] || return 1
    cleanup_retired_job "$retired"
}

cleanup_retired_job() {
    # Only atomically retired, fully published generated jobs reach this path.
    [[ "$1" == "$WORK_DIR/"*/.completed.recording.* ]] || return 1
    find "$1" -depth -delete
}

new_recording_job() {
    local title="$1" author="$2" stream_id="$3" stem season episode
    RECORDING_JOB=$(mktemp -d "$WORK_DIR/active/recording.XXXXXXXX") || return 1
    season=$(date +%Y%m)
    episode=$(date +%d%H)
    # Reserve bytes for extension and a unique recording identifier; truncate
    # only the title, without cutting a UTF-8 codepoint in half.
    stem=$(python3 - "$author" "$title" "$season" "$episode" "$MODE" \
        "$stream_id" "${RECORDING_JOB##*/}" <<'PY'
import sys
author, title, season, episode, mode, stream, identifier = sys.argv[1:]
prefix = f'{author.replace("/", "-")} - s{season}e{episode} - '
suffix = f' - {{edition-{mode}}} - {stream}-{identifier}'
budget = 224 - len((prefix + suffix).encode('utf-8'))
if budget < 0:
    raise SystemExit('Recording identity exceeds filename limit')
safe = title.replace('/', '-').encode('utf-8')[:budget].decode('utf-8', 'ignore')
print(prefix + safe + suffix, end='')
PY
    ) || return 1
    printf '%s\0' "$stem" > "$RECORDING_JOB/stem" &&
        printf '%s\0' "$title" > "$RECORDING_JOB/title" &&
        printf '%s\n' "$season" > "$RECORDING_JOB/season" &&
        printf '%s\n' "$(date +%s)" > "$RECORDING_JOB/started-at" &&
        date +%m/%d/%Y > "$RECORDING_JOB/recorded-date" &&
        printf '%s\n' "${REMUX:-false}" > "$RECORDING_JOB/remux-required" &&
        printf '%s\n' "${UPLOAD:-false}" > "$RECORDING_JOB/upload-required" &&
        printf '%s\n' "${ENCODE:-false}" > "$RECORDING_JOB/encode-required" &&
        write_state "$WORK_DIR/active.job" "$RECORDING_JOB"
}

advance_job() {
    local job="$1" stage="$2" target
    target="$WORK_DIR/$stage/${job##*/}"
    [[ ! -e "$target" ]] || return 1
    mv -T -n -- "$job" "$target" && [[ ! -d "$job" ]]
}

route_recording() {
    local job="$1" upload encode
    read -r upload < "$job/upload-required" &&
        read -r encode < "$job/encode-required" || return 1
    if [[ "$upload" == true ]]; then
        advance_job "$job" upload
    elif [[ "$encode" == true ]]; then
        advance_job "$job" encode
    else
        advance_job "$job" publish
    fi
}

complete_recording() {
    local input
    if [[ -f "$1/capture.bin" ]]; then input="$1/capture.bin"; else input=$(job_media "$1") || return 1; fi
    [[ -s "$input" ]] || return 1
    rm -f -- "$WORK_DIR/active.job"
    write_state "$1/completed" true && advance_job "$1" normalize
}

normalize_job() {
    ensure_normalized_job "$1" && route_recording "$1"
}

recover_completed_recordings() {
    local job input
    while IFS= read -r -d '' job; do
        if [[ -f "$job/completed" ]]; then
            advance_job "$job" normalize || log_failure 'Completed recording pending' "$job"
        elif [[ "${AUTO_RECOVER_INTERRUPTED:-false}" == true ]]; then
            if [[ -f "$job/capture.bin" ]]; then input="$job/capture.bin"; else input=$(job_media "$job" 2>/dev/null || true); fi
            if [[ -n "$input" && -s "$input" ]]; then
                printf '[Pipeline] Auto-recovering orphaned active capture: %q\n' "$job" >&2
                complete_recording "$job" || log_failure 'Active recording recovery pending' "$job"
            fi
        fi
    done < <(find "$WORK_DIR/active" -mindepth 1 -maxdepth 1 -type d -print0)
}

publish_recording() {
    local job="$1" id input
    ensure_normalized_job "$job" || return 1
    while IFS= read -r id; do
        input=$(part_file "$job" "$id") || return 1
        publish_part "$job" "$id" "$input" || return 1
    done < <(part_ids "$job")
    remove_completed_job "$job" || return 1
    prune_old_recordings
}

# flock is released by the kernel after a crash; no stale PID files or claims.
# Each scan holds the stage lock through processing, preventing duplicate workers.
run_queue_once() (
    local stage="$1" handler="$2" job now retry_at attempts delay
    exec 8> "$WORK_DIR/$stage.lock"
    flock -n 8 || return 0
    while IFS= read -r -d '' job; do
        cleanup_retired_job "$job" || log_failure 'Completed job cleanup pending' "$job"
    done < <(find "$WORK_DIR/$stage" -mindepth 1 -maxdepth 1 -type d -name '.completed.recording.*' -print0)
    while IFS= read -r -d '' job; do
        [[ -d "$job" ]] || continue
        now=$(date +%s)
        retry_at=0
        if [[ -f "$job/$stage.retry-at" ]]; then
            read -r retry_at < "$job/$stage.retry-at" || retry_at=0
        fi
        [[ "$retry_at" =~ ^[0-9]+$ ]] || retry_at=0
        (( now >= retry_at )) || continue
        if "$handler" "$job"; then
            continue
        fi
        # A successful stage transfer may have already removed the source path.
        [[ -d "$job" ]] || continue
        attempts=0
        if [[ -f "$job/$stage.attempts" ]]; then
            read -r attempts < "$job/$stage.attempts" || attempts=0
        fi
        [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=0
        (( attempts < 6 )) && attempts=$((attempts + 1))
        delay=$((30 * (2 ** (attempts - 1))))
        (( delay <= 900 )) || delay=900
        if [[ -f "$job/$stage.retry-at" ]]; then
            read -r current_retry < "$job/$stage.retry-at" || current_retry=0
            if [[ "$current_retry" =~ ^[0-9]+$ ]] && (( current_retry > now + delay )); then
                delay=$((current_retry - now))
            fi
        fi
        write_state "$job/$stage.attempts" "$attempts" || return 1
        write_state "$job/$stage.retry-at" "$((now + delay))" || return 1
        log_failure "Stage $stage failed; retry in ${delay}s" "$job"
    done < <(find "$WORK_DIR/$stage" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' -print0)
)

run_worker() {
    local stage="$1" handler="$2"
    pipeline_init || return 1
    while true; do
        run_queue_once "$stage" "$handler" || return 1
        [[ "${WORKER_ONCE:-false}" != true ]] || return 0
        sleep 5
    done
}
