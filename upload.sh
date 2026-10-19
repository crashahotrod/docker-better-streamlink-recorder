#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"

invoke_uploader() {
    timeout --signal=TERM --kill-after=30s 6h "${YOUTUBEUPLOADER_BIN:-/etc/youtubeuploader}" "$@"
}

upload_job() {
    local job="$1" input title recorded_date encode
    input=$(job_media "$job") && read_field "$job/title" title || return 1
    read -r recorded_date < "$job/recorded-date" || return 1
    if [[ ! -f "$job/uploaded" ]]; then
        validate_media "$input" || return 1
        if ! invoke_uploader -title "$recorded_date - $title" -privacy "${YOUTUBE_PRIVACY:-public}" \
            -filename "$input" -description "Uploaded Automatically by ${UPLOAD_BOT_NAME:?}" \
            -secrets "${CONFIG_DIR:-/config}/youtubeuploader_client_secrets.json" \
            -cache "${CONFIG_DIR:-/config}/youtubeuploader_request.token" \
            -metaJSONout "$job/remote.json"; then
            log_failure 'Upload failed; recording retained' "$job"
            return 1
        fi
        # Persist success before any local handoff. remote.json is retained for
        # recovery; a crash before this marker can still require reconciliation.
        write_state "$job/uploaded" true || return 1
        printf '[Upload] Upload completed: %q\n' "$job"
    fi
    read -r encode < "$job/encode-required" || return 1
    if [[ "$encode" == true ]]; then
        advance_job "$job" encode
    else
        advance_job "$job" publish
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    run_worker upload upload_job
fi
