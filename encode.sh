#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

encode_job() {
    local job="$1" input output="$1/encoded.partial.mp4"
    if recover_publication "$job"; then return 0; fi
    input=$(job_media "$job") || return 1
    # An interrupted attempt's output is never a new queue item.
    if ! ffmpeg -nostdin -y -v error -i "$input" -c:v libx264 -crf 23 \
        -preset medium -c:a aac -b:a 128k -f mp4 "$output"; then
        log_failure 'Encoding failed; source retained' "$job"
        return 1
    fi
    if ! validate_media "$output"; then
        log_failure 'Encoded output invalid; source retained' "$job"
        return 1
    fi
    if ! publish_job "$job" "$output" mp4; then
        log_failure 'Publication failed; source retained' "$job"
        return 1
    fi
    remove_completed_job "$job"
}

encode_main() {
    run_worker encode encode_job
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    encode_main
fi
