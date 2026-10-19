#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

get_channel_status() {
    local active_job=""
    if [[ -f "$WORK_DIR/active.job" ]]; then
        read -r active_job < "$WORK_DIR/active.job" || active_job=""
        if [[ -n "$active_job" && -d "$active_job" ]]; then
            printf 'recording\n'
            return 0
        fi
    fi
    if [[ -f "$WORK_DIR/channel.status" ]]; then
        local st=""
        read -r st < "$WORK_DIR/channel.status" || st=""
        if [[ "$st" == "recording" || "$st" == "monitoring" || "$st" == "offline" ]]; then
            if [[ "$st" != "recording" ]]; then
                printf '%s\n' "$st"
                return 0
            fi
        fi
    fi
    # If download.lock is held, download process is active
    (
        exec 9< "$WORK_DIR/download.lock"
        if flock -n 9; then
            printf 'offline\n'
        else
            printf 'monitoring\n'
        fi
    ) 2>/dev/null || printf 'offline\n'
}

build_status_json() {
    local now channel_status active_json="null"
    now=$(date +%s)
    channel_status=$(get_channel_status)

    local active_job=""
    if [[ -f "$WORK_DIR/active.job" ]]; then
        read -r active_job < "$WORK_DIR/active.job" || active_job=""
    fi

    if [[ -n "$active_job" && -d "$active_job" ]]; then
        local job_id="${active_job##*/}"
        local title="Untitled Stream"
        if [[ -f "$active_job/title" ]]; then
            read_field "$active_job/title" title || title="Untitled Stream"
        fi
        local author="${CHANNEL:-unknown}"
        local started_at=$now
        if [[ -f "$active_job/started-at" ]]; then
            read -r started_at < "$active_job/started-at" || started_at=$now
        fi
        [[ "$started_at" =~ ^[0-9]+$ ]] || started_at=$now
        local duration_seconds=$((now - started_at))
        (( duration_seconds >= 0 )) || duration_seconds=0
        local duration_human
        duration_human=$(format_duration "$duration_seconds")

        local capture_file="$active_job/capture.bin"
        local size_bytes=0
        if [[ -f "$capture_file" ]]; then
            size_bytes=$(stat -c %s "$capture_file" 2>/dev/null || stat -f %z "$capture_file" 2>/dev/null || wc -c < "$capture_file" 2>/dev/null || echo 0)
        elif [[ -f "$active_job/media.mp4" ]]; then
            size_bytes=$(stat -c %s "$active_job/media.mp4" 2>/dev/null || stat -f %z "$active_job/media.mp4" 2>/dev/null || wc -c < "$active_job/media.mp4" 2>/dev/null || echo 0)
        elif [[ -f "$active_job/media.ts" ]]; then
            size_bytes=$(stat -c %s "$active_job/media.ts" 2>/dev/null || stat -f %z "$active_job/media.ts" 2>/dev/null || wc -c < "$active_job/media.ts" 2>/dev/null || echo 0)
        fi
        [[ "$size_bytes" =~ ^[0-9]+$ ]] || size_bytes=0
        local size_human
        size_human=$(format_bytes "$size_bytes")

        active_json=$(jq -n \
            --arg job_id "$job_id" \
            --arg title "$title" \
            --arg author "$author" \
            --argjson started_at "$started_at" \
            --argjson duration_seconds "$duration_seconds" \
            --arg duration_human "$duration_human" \
            --argjson size_bytes "$size_bytes" \
            --arg size_human "$size_human" \
            '{
                job_id: $job_id,
                title: $title,
                author: $author,
                started_at: $started_at,
                duration_seconds: $duration_seconds,
                duration_human: $duration_human,
                size_bytes: $size_bytes,
                size_human: $size_human
            }')
    fi

    local q_active q_normalize q_upload q_encode q_publish q_quarantine
    q_active=$(find "$WORK_DIR/active" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' 2>/dev/null | wc -l)
    q_normalize=$(find "$WORK_DIR/normalize" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' 2>/dev/null | wc -l)
    q_upload=$(find "$WORK_DIR/upload" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' 2>/dev/null | wc -l)
    q_encode=$(find "$WORK_DIR/encode" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' 2>/dev/null | wc -l)
    q_publish=$(find "$WORK_DIR/publish" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' 2>/dev/null | wc -l)
    q_quarantine=$(find "$WORK_DIR/quarantine" -mindepth 1 -maxdepth 1 -type d -name 'recording.*' 2>/dev/null | wc -l)

    local scratch_kb storage_kb scratch_free_gb=0 storage_free_gb=0 min_free_gb="${MIN_FREE_SPACE_GB:-0}"
    scratch_kb=$(df -k --output=avail "${SCRATCH_ROOT:-/etc/streamlink/scratch}" 2>/dev/null | tail -n 1 | tr -d ' ') || scratch_kb=0
    storage_kb=$(df -k --output=avail "${STORAGE_DIR:-/storage}" 2>/dev/null | tail -n 1 | tr -d ' ') || storage_kb=0
    [[ "$scratch_kb" =~ ^[0-9]+$ ]] && scratch_free_gb=$((scratch_kb / 1024 / 1024))
    [[ "$storage_kb" =~ ^[0-9]+$ ]] && storage_free_gb=$((storage_kb / 1024 / 1024))
    [[ "$min_free_gb" =~ ^[0-9]+$ ]] || min_free_gb=0

    jq -n \
        --arg channel "${CHANNEL:-unknown}" \
        --arg mode "${MODE:-unknown}" \
        --arg status "$channel_status" \
        --argjson active_capture "$active_json" \
        --argjson q_active "$q_active" \
        --argjson q_normalize "$q_normalize" \
        --argjson q_upload "$q_upload" \
        --argjson q_encode "$q_encode" \
        --argjson q_publish "$q_publish" \
        --argjson q_quarantine "$q_quarantine" \
        --argjson scratch_free_gb "$scratch_free_gb" \
        --argjson storage_free_gb "$storage_free_gb" \
        --argjson min_free_space_gb "$min_free_gb" \
        '{
            channel: $channel,
            mode: $mode,
            status: $status,
            updated_at: (now | todate),
            active_capture: $active_capture,
            queues: {
                active: $q_active,
                normalize: $q_normalize,
                upload: $q_upload,
                encode: $q_encode,
                publish: $q_publish,
                quarantine: $q_quarantine
            },
            disk_space: {
                scratch_free_gb: $scratch_free_gb,
                storage_free_gb: $storage_free_gb,
                min_free_space_gb: $min_free_space_gb
            }
        }'
}

write_status_files() {
    local json="$1"
    local scratch_dir="${SCRATCH_ROOT:-/etc/streamlink/scratch}"
    local scratch_file="$scratch_dir/status.json"
    local work_file="$WORK_DIR/status.json"

    mkdir -p -- "$scratch_dir" "$WORK_DIR" 2>/dev/null || true

    printf '%s\n' "$json" > "$scratch_file.tmp" && mv -f -- "$scratch_file.tmp" "$scratch_file" || true
    printf '%s\n' "$json" > "$work_file.tmp" && mv -f -- "$work_file.tmp" "$work_file" || true
}

build_persistent_discord_payload() {
    local status_json="$1"
    local channel status updated_at
    channel=$(jq -r '.channel' <<< "$status_json")
    status=$(jq -r '.status' <<< "$status_json")
    updated_at=$(jq -r '.updated_at' <<< "$status_json")

    local status_display color
    case "$status" in
        recording)
            status_display="🔴 Recording"
            color=15158332
            ;;
        monitoring)
            status_display="🟢 Monitoring"
            color=3447003
            ;;
        *)
            status_display="⚪ Offline"
            color=9807270
            ;;
    esac

    local fields
    fields=$(jq -n \
        --arg status_display "$status_display" \
        --argjson s "$status_json" \
        '[
            {
                name: "Channel Status",
                value: $status_display,
                inline: true
            },
            {
                name: "Platform",
                value: ($s.mode | ascii_upcase),
                inline: true
            },
            (if $s.active_capture != null then
                {
                    name: "Active Recording",
                    value: ("**Title**: " + $s.active_capture.title + "\n**Duration**: " + $s.active_capture.duration_human + "\n**Current Size**: " + $s.active_capture.size_human),
                    inline: false
                }
            else empty end),
            {
                name: "Pipeline Queues",
                value: ("Active: " + ($s.queues.active | tostring) + " | Normalize: " + ($s.queues.normalize | tostring) + " | Upload: " + ($s.queues.upload | tostring) + "\nEncode: " + ($s.queues.encode | tostring) + " | Publish: " + ($s.queues.publish | tostring) + " | Quarantine: " + ($s.queues.quarantine | tostring)),
                inline: false
            },
            {
                name: "Disk Space",
                value: ("Scratch: " + ($s.disk_space.scratch_free_gb | tostring) + " GB | Storage: " + ($s.disk_space.storage_free_gb | tostring) + " GB (Min: " + ($s.disk_space.min_free_space_gb | tostring) + " GB)"),
                inline: false
            }
        ]') || return 1

    jq -n \
        --arg title "📊 Streamlink Recorder Status: $channel" \
        --argjson color "$color" \
        --argjson fields "$fields" \
        --arg updated "$updated_at" \
        '{
            embeds: [{
                title: $title,
                color: $color,
                fields: $fields,
                footer: { text: ("Last updated: " + $updated) },
                timestamp: $updated
            }]
        }'
}

update_persistent_discord() {
    local status_json="$1"
    local webhook
    webhook=$(get_monitor_webhook)
    [[ -n "$webhook" ]] || return 0
    webhook="${webhook%/}"

    local id_file="$WORK_DIR/status_message.id"
    local payload
    payload=$(build_persistent_discord_payload "$status_json") || return 1

    local now msg_id="" http_code response body
    now=$(date +%s)

    if [[ -f "$id_file" ]]; then
        read -r msg_id < "$id_file" || msg_id=""
    fi

    if [[ -n "$msg_id" ]]; then
        local patch_url="${webhook}/messages/${msg_id}"
        response=$(curl -s -w '\n%{http_code}' --connect-timeout 10 --max-time 30 -X PATCH \
            -H 'Content-Type: application/json' --data "$payload" \
            "$patch_url" 2>/dev/null || printf '\n000')
        http_code=$(tail -n 1 <<< "$response")
        if [[ "$http_code" == "200" ]]; then
            write_state "$WORK_DIR/.last_discord_status_time" "$now"
            return 0
        elif [[ "$http_code" == "404" ]]; then
            printf '[Discord Status] Message %s was deleted (404); recreating persistent status message.\n' "$msg_id" >&2
            rm -f -- "$id_file"
            msg_id=""
        else
            printf '[Discord Status] Failed to update message %s (HTTP %s).\n' "$msg_id" "$http_code" >&2
            return 1
        fi
    fi

    local post_url
    if [[ "$webhook" == *\?* ]]; then
        post_url="${webhook}&wait=true"
    else
        post_url="${webhook}?wait=true"
    fi

    response=$(curl -s -w '\n%{http_code}' --connect-timeout 10 --max-time 30 -X POST \
        -H 'Content-Type: application/json' --data "$payload" \
        "$post_url" 2>/dev/null || printf '\n000')
    http_code=$(tail -n 1 <<< "$response")
    body=$(sed '$d' <<< "$response")

    if [[ "$http_code" == "200" || "$http_code" == "201" ]]; then
        msg_id=$(jq -r '.id // empty' <<< "$body" 2>/dev/null || true)
        if [[ -n "$msg_id" ]]; then
            write_state "$id_file" "$msg_id"
            write_state "$WORK_DIR/.last_discord_status_time" "$now"
            printf '[Discord Status] Created persistent status message: %s\n' "$msg_id" >&2
            return 0
        fi
    fi

    printf '[Discord Status] Failed to create persistent status message (HTTP %s).\n' "$http_code" >&2
    return 1
}

status_cycle() {
    local status_json data_fp now last_time=0 last_fp="" time_diff should_update=false
    status_json=$(build_status_json) || return 1
    write_status_files "$status_json"

    local webhook
    webhook=$(get_monitor_webhook)
    [[ -n "$webhook" ]] || return 0

    data_fp=$(jq -c '{
        status: .status,
        job: .active_capture.job_id,
        title: .active_capture.title,
        queues: .queues,
        disk: .disk_space
    }' <<< "$status_json" 2>/dev/null || echo "")

    now=$(date +%s)
    if [[ -f "$WORK_DIR/.last_discord_status_time" ]]; then
        read -r last_time < "$WORK_DIR/.last_discord_status_time" || last_time=0
    fi
    [[ "$last_time" =~ ^[0-9]+$ ]] || last_time=0
    time_diff=$((now - last_time))

    if [[ -f "$WORK_DIR/.last_discord_status_fp" ]]; then
        read -r last_fp < "$WORK_DIR/.last_discord_status_fp" || last_fp=""
    fi

    if [[ "${STATUS_ONCE:-false}" == true || ! -f "$WORK_DIR/status_message.id" ]]; then
        should_update=true
    elif (( time_diff >= 30 )); then
        should_update=true
    elif [[ "$data_fp" != "$last_fp" ]] && (( time_diff >= 5 )); then
        should_update=true
    fi

    if [[ "$should_update" == true ]]; then
        if update_persistent_discord "$status_json"; then
            write_state "$WORK_DIR/.last_discord_status_fp" "$data_fp"
        fi
    fi
}

status_main() {
    pipeline_init || return 1
    while true; do
        status_cycle || true
        [[ "${STATUS_ONCE:-false}" != true ]] || break
        sleep "${STATUS_INTERVAL:-5}"
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    status_main
fi
