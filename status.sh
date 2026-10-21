#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

recorder_running() {
    [[ -f "$WORK_DIR/download.lock" ]] || return 1
    # A distinct contention exit code separates a held lock from an I/O error.
    if flock -n -E 3 "$WORK_DIR/download.lock" true; then return 1; else [[ $? -eq 3 ]]; fi
}

capture_activity_time() {
    stat -c %Y "$1/capture.bin" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || printf '0\n'
}

get_channel_status() {
    local active_job='' st='' heartbeat=0 now activity stale="${RECORDER_STALE_SECONDS:-180}"
    local poll_interval="${CHECK_INTERVAL:-30}"
    recorder_running || { printf 'offline\n'; return 0; }
    now=$(date +%s)
    if [[ -f "$WORK_DIR/channel.status" ]]; then read -r st < "$WORK_DIR/channel.status" || st=''; fi
    if [[ "$st" == recording ]]; then
        if [[ -f "$WORK_DIR/active.job" ]]; then read -r active_job < "$WORK_DIR/active.job" || active_job=''; fi
        if [[ "$active_job" == "$WORK_DIR/active/recording."* && -d "$active_job" ]]; then
            activity=$(capture_activity_time "$active_job")
            if (( now - activity <= stale )); then printf 'recording\n'; else printf 'error\n'; fi
        else
            printf 'error\n'
        fi
        return 0
    fi
    if [[ -f "$WORK_DIR/recorder.heartbeat" ]]; then read -r heartbeat < "$WORK_DIR/recorder.heartbeat" || heartbeat=0; fi
    [[ "$heartbeat" =~ ^[0-9]+$ ]] || heartbeat=0
    # A longer configured poll wait is expected inactivity, not a stuck monitor.
    if (( now - heartbeat > stale + poll_interval )); then
        printf 'error\n'
    else
        case "$st" in offline|monitoring|error) printf '%s\n' "$st" ;; *) printf 'error\n' ;; esac
    fi
}

build_status_json() {
    local now channel_status active_json="null" heartbeat=0
    now=$(date +%s)
    channel_status=$(get_channel_status)
    if [[ -f "$WORK_DIR/recorder.heartbeat" ]]; then read -r heartbeat < "$WORK_DIR/recorder.heartbeat" || heartbeat=0; fi
    [[ "$heartbeat" =~ ^[0-9]+$ ]] || heartbeat=0

    local active_job=""
    if [[ -f "$WORK_DIR/active.job" ]]; then
        read -r active_job < "$WORK_DIR/active.job" || active_job=""
    fi

    if [[ "$channel_status" != offline && "$active_job" == "$WORK_DIR/active/recording."* && -d "$active_job" ]]; then
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
        local last_growth_at stalled=false
        last_growth_at=$(capture_activity_time "$active_job")
        (( now - last_growth_at <= ${RECORDER_STALE_SECONDS:-180} )) || stalled=true

        local segment_index=0 total_duration_seconds=0 total_duration_human="" parent_id=""
        if [[ -f "$active_job/segment-index" ]]; then
            read -r segment_index < "$active_job/segment-index" || segment_index=0
            [[ "$segment_index" =~ ^[0-9]+$ ]] || segment_index=0
        fi
        if [[ -f "$active_job/segment-parent" ]]; then
            read -r parent_id < "$active_job/segment-parent" || parent_id=""
            if [[ -n "$parent_id" && -f "$WORK_DIR/sessions/recording.$parent_id/started-at" ]]; then
                local session_started=0
                read -r session_started < "$WORK_DIR/sessions/recording.$parent_id/started-at" || session_started=0
                if [[ "$session_started" =~ ^[0-9]+$ ]] && (( session_started > 0 )); then
                    total_duration_seconds=$((now - session_started))
                    (( total_duration_seconds >= 0 )) || total_duration_seconds=0
                    total_duration_human=$(format_duration "$total_duration_seconds")
                fi
            fi
        fi

        active_json=$(jq -n \
            --arg job_id "$job_id" \
            --arg title "$title" \
            --arg author "$author" \
            --argjson started_at "$started_at" \
            --argjson duration_seconds "$duration_seconds" \
            --arg duration_human "$duration_human" \
            --argjson size_bytes "$size_bytes" \
            --arg size_human "$size_human" \
            --argjson last_growth_at "$last_growth_at" \
            --argjson stalled "$stalled" \
            --argjson segment_index "$segment_index" \
            --argjson total_duration_seconds "$total_duration_seconds" \
            --arg total_duration_human "$total_duration_human" \
            '{
                job_id: $job_id,
                title: $title,
                author: $author,
                started_at: $started_at,
                duration_seconds: $duration_seconds,
                duration_human: $duration_human,
                size_bytes: $size_bytes,
                size_human: $size_human,
                last_growth_at: $last_growth_at,
                stalled: $stalled,
                segment_index: (if $segment_index > 0 then $segment_index else null end),
                total_duration_seconds: (if $total_duration_seconds > 0 then $total_duration_seconds else null end),
                total_duration_human: (if $total_duration_human != "" then $total_duration_human else null end)
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
        --argjson heartbeat "$heartbeat" \
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
            recorder_heartbeat_at: $heartbeat,
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
    local scratch_file="$scratch_dir/status-${CHANNEL}-${MODE}.json"
    local work_file="$WORK_DIR/status.json"

    mkdir -p -- "$scratch_dir" "$WORK_DIR" 2>/dev/null || true

    write_state "$scratch_file" "$json" || true
    write_state "$work_file" "$json" || true
}

status_cycle() {
    local status_json
    status_json=$(build_status_json) || return 1
    write_status_files "$status_json"
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
