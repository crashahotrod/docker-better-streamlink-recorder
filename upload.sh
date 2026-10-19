#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

invoke_uploader() {
    timeout --signal=TERM --kill-after=30s 6h "${YOUTUBEUPLOADER_BIN:-/etc/youtubeuploader}" "$@"
}

upload_job() {
    local job="$1" id input title recorded_date encode count part_title
    ensure_normalized_job "$job" || return 1
    read_field "$job/title" title && read -r recorded_date < "$job/recorded-date" || return 1
    count=$(jq '.parts | length' "$job/normalized.json") || return 1
    while IFS= read -r id; do
        input=$(part_file "$job" "$id") || return 1
        if [[ -f "$job/parts/$id.uploaded" ]]; then continue; fi
        part_title="$recorded_date - $title"
        if (( count > 1 )); then part_title="$part_title - part $id"; fi
        if ! invoke_uploader -title "$part_title" -privacy "${YOUTUBE_PRIVACY:-public}" \
            -filename "$input" -description "Uploaded Automatically by ${UPLOAD_BOT_NAME:?}" \
            -secrets "${CONFIG_DIR:-/config}/youtubeuploader_client_secrets.json" \
            -cache "${CONFIG_DIR:-/config}/youtubeuploader_request.token" \
            -metaJSONout "$job/parts/$id.remote.json"; then
            log_failure 'Upload failed; capture and per-part receipts retained' "$job"
            return 1
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
