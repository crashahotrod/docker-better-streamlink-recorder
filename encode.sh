#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

encode_job() {
    local job="$1" id input output
    ensure_normalized_job "$job" || return 1
    while IFS= read -r id; do
        if part_committed "$job" "$id" mp4; then continue; fi
        input=$(part_file "$job" "$id") || return 1
        output="$job/parts/$id.encoded.mp4"
        if ! ffmpeg -nostdin -y -v warning -i "$input" -map '0:v?' -map '0:a?' \
            -fps_mode passthrough -enc_time_base:v -1 -c:v libx264 -crf 23 -preset medium -c:a aac \
            -b:a 128k -f mp4 "$output" 2> "$job/diagnostics/encode-$id.stderr"; then
            log_failure 'Encoding failed; capture retained' "$job"
            return 1
        fi
        verify_part "$job" "$id" "$output" --encoded || return 1
        publish_part "$job" "$id" "$output" || return 1
    done < <(part_ids "$job")
    remove_completed_job "$job"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    run_worker encode encode_job
fi
