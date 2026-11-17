#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"
pipeline_init || exit 1
while true; do
    prune_quarantine || true
    python3 "$(dirname -- "${BASH_SOURCE[0]}")/segment_capture.py" audit "$WORK_DIR" || true
    [[ "${WORKER_ONCE:-false}" != true ]] || break
    sleep 30
done
