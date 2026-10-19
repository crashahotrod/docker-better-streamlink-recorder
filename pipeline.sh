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
    ln -- "$temporary/media.$extension" "$final"
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
        date +%m/%d/%Y > "$RECORDING_JOB/recorded-date" &&
        printf '%s\n' "${REMUX:-false}" > "$RECORDING_JOB/remux-required" &&
        printf '%s\n' "${UPLOAD:-false}" > "$RECORDING_JOB/upload-required" &&
        printf '%s\n' "${ENCODE:-false}" > "$RECORDING_JOB/encode-required"
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
    write_state "$1/completed" true && advance_job "$1" normalize
}

normalize_job() {
    ensure_normalized_job "$1" && route_recording "$1"
}

recover_completed_recordings() {
    local job
    while IFS= read -r -d '' job; do
        if [[ -f "$job/completed" ]]; then
            advance_job "$job" normalize || log_failure 'Completed recording pending' "$job"
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
    remove_completed_job "$job"
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
