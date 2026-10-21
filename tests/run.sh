#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$ROOT"
case "${1:-}" in
    ''|--lint-only)
        for script in ./*.sh tests/*.sh; do bash -n "$script"; done
        shellcheck -x -P SCRIPTDIR ./*.sh tests/*.sh
        [[ "${1:-}" != --lint-only ]] || exit 0
        ;;
    --behavior-only) ;;
    *) echo 'Usage: tests/run.sh [--behavior-only|--lint-only]' >&2; exit 1 ;;
esac
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v
bash tests/test_pipeline.sh
bash tests/test_download.sh
bash tests/test_catchup.sh
bash tests/test_extra_args.sh
bash tests/test_encoding.sh
bash tests/test_queue.sh
bash tests/test_startup.sh
bash tests/test_notifications.sh
bash tests/test_upload_operations.sh
bash tests/test_media.sh
