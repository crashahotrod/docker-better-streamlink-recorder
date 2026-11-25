#!/bin/bash
set -uo pipefail
# shellcheck source=pipeline.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/pipeline.sh"
run_worker normalize normalize_job
