#!/bin/bash
# Offline recovery: write new parts, retaining the original capture and metadata.
set -euo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"
pipeline_init
[[ $# -eq 1 ]] || { echo 'Usage: recover.sh /full/path/to/recording.JOB' >&2; exit 1; }
job=$(realpath -e -- "$1")
[[ "$job" == "$WORK_DIR/"*/recording.* && -d "$job" ]] || exit 1
if [[ "${job%/*}" == "$WORK_DIR/sessions" ]]; then
    exec 9> "$WORK_DIR/download.lock"
    flock -n 9 || { echo 'Stop the recorder before recovering a segmented session.' >&2; exit 1; }
    exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/session_recovery.py" recover "$WORK_DIR" "$job"
fi
# Retention and manual quarantine recovery must not process the same job concurrently.
exec 6> "$WORK_DIR/quarantine.lock"
flock -n 6 || { echo 'Quarantine cleanup is active; retry recovery shortly.' >&2; exit 1; }
[[ -f "$job/completed" ]] || { echo 'Inspect interrupted captures and explicitly mark completion first.' >&2; exit 1; }
[[ ! -f "$job/normalized.json" ]] || { echo 'Existing manifest retained; reconcile this job manually.' >&2; exit 1; }
media_command recover "$job"
if [[ "${job%/*}" != "$WORK_DIR/normalize" ]]; then advance_job "$job" normalize; fi
printf 'Verified candidates queued for processing; original capture retained.\n'
