#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

encode_job() {
    local job="$1" id input output
    local abitrate arg custom_filter=false
    local -a extra_encode_args=()
    abitrate="${ENCODE_AUDIO_BITRATE:-128k}"
    build_encoding_args || return 1
    if [[ -n "${ENCODE_EXTRA_ARGS:-}" ]]; then
        load_extra_args extra_encode_args "$ENCODE_EXTRA_ARGS" || return 1
    fi
    if [[ "$ENCODER_ACCEL" == vaapi ]]; then
        for arg in "${extra_encode_args[@]}"; do
            case "$arg" in
                -vf|-vf=*|-filter:v*|-filter_complex*|-lavfi) custom_filter=true ;;
            esac
        done
        # Custom graphs take precedence and must supply their own hardware upload.
        if [[ "$custom_filter" == false ]]; then
            ENCODER_VIDEO_ARGS+=(-vf 'format=nv12,hwupload')
        fi
    fi
    ensure_normalized_job "$job" || return 1
    while IFS= read -r id; do
        if part_committed "$job" "$id" mp4; then continue; fi
        input=$(part_file "$job" "$id") || return 1
        output="$job/parts/$id.encoded.mp4"
        if ! ffmpeg -nostdin -y -v warning "${ENCODER_INPUT_ARGS[@]}" -i "$input" -map '0:v?' -map '0:a?' \
            -fps_mode passthrough -enc_time_base:v -1 "${ENCODER_VIDEO_ARGS[@]}" -c:a aac \
            -b:a "$abitrate" "${extra_encode_args[@]}" -f mp4 "$output" 2> "$job/diagnostics/encode-$id.stderr"; then
            log_failure 'Encoding failed; capture retained' "$job"
            return 1
        fi
        verify_part "$job" "$id" "$output" --encoded || return 1
        publish_part "$job" "$id" "$output" || return 1
    done < <(part_ids "$job")
    remove_completed_job "$job" || return 1
    prune_old_recordings
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    run_worker encode encode_job
fi
