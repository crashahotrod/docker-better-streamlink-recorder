#!/bin/bash
# Shared queue and publication operations. Callers handle failures explicitly.

pipeline_init() {
    WORK_DIR="${SCRATCH_ROOT:-/etc/streamlink/scratch}/${MODE:?}/${CHANNEL:?}/queue-v2"
    STORAGE_DIR="${STORAGE_DIR:-/storage}"
    mkdir -p -- "$WORK_DIR"/{active,upload,encode,publish} || return 1
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

validate_media() {
    local kind
    [[ -s "$1" ]] || return 1
    kind=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_type \
        -of json "$1") || return 1
    jq -e '.streams | any(.codec_type == "video")' <<< "$kind" > /dev/null
}

file_hash() {
    sha256sum < "$1" | cut -d ' ' -f 1
}

# Temporary copy and final hard link share a filesystem. Linking commits without
# overwriting an existing name, including when another process publishes first.
publish_job() (
    local job="$1" input="$2" extension="$3" stem title season destination final
    local temporary='' checksum
    read_field "$job/stem" stem && read_field "$job/title" title || return 1
    read -r season < "$job/season" || return 1
    [[ "$season" =~ ^[0-9]{6}$ && "$stem" != */* && -n "$stem" ]] || return 1
    destination="$STORAGE_DIR/$CHANNEL/Season $season"
    final="$destination/$stem.$extension"
    mkdir -p -- "$destination" || return 1
    if [[ -e "$final" ]]; then
        [[ -f "$job/published.sha256" ]] || return 1
        read -r checksum < "$job/published.sha256" || return 1
        [[ "$checksum" == "$(file_hash "$final")" ]]
        return
    fi
    validate_media "$input" || return 1
    temporary=$(mktemp -d "$destination/.publish.XXXXXXXX") || return 1
    trap 'rm -f -- "$temporary/media.$extension"; rmdir -- "$temporary"' EXIT
    cp -- "$input" "$temporary/media.$extension" || return 1
    cmp -s -- "$input" "$temporary/media.$extension" || return 1
    if [[ "$extension" == mp4 ]]; then
        exiftool -q -q -title="$title" -api largefilesupport=1 \
            -overwrite_original "$temporary/media.$extension" || return 1
    fi
    validate_media "$temporary/media.$extension" || return 1
    checksum=$(file_hash "$temporary/media.$extension") || return 1
    write_state "$job/published.extension" "$extension" &&
        write_state "$job/published.sha256" "$checksum" || return 1
    ln -- "$temporary/media.$extension" "$final" || return 1
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
    # Generated queue directory; called only after publication has succeeded.
    local job="$1" retired
    [[ "$job" == "$WORK_DIR/"* && "$job" != "$WORK_DIR/" ]] || return 1
    retired="${job%/*}/.completed.${job##*/}"
    [[ ! -e "$retired" ]] || return 1
    mv -T -n -- "$job" "$retired" && [[ ! -d "$job" ]] || return 1
    cleanup_retired_job "$retired"
}

cleanup_retired_job() {
    find "$1" -maxdepth 1 -type f -exec rm -- {} + && rmdir -- "$1"
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
budget = 240 - len((prefix + suffix).encode('utf-8'))
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
    input=$(job_media "$1") && validate_media "$input" || return 1
    write_state "$1/completed" true && route_recording "$1"
}

recover_completed_recordings() {
    local job
    while IFS= read -r -d '' job; do
        if [[ -f "$job/completed" ]]; then
            route_recording "$job" || log_failure 'Completed recording pending' "$job"
        fi
    done < <(find "$WORK_DIR/active" -mindepth 1 -maxdepth 1 -type d -print0)
}

publish_recording() {
    local input
    if recover_publication "$1"; then return 0; fi
    input=$(job_media "$1") || return 1
    publish_job "$1" "$input" "${input##*.}" && remove_completed_job "$1"
}

recover_publication() {
    local extension
    [[ -f "$1/published.extension" && -f "$1/published.sha256" ]] || return 1
    read -r extension < "$1/published.extension" || return 1
    [[ "$extension" == mp4 || "$extension" == ts ]] || return 1
    # publish_job checks a committed destination before reading its input.
    publish_job "$1" '' "$extension" && remove_completed_job "$1"
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
