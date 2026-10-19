#!/bin/bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$ROOT"
for script in ./*.sh tests/*.sh; do bash -n "$script"; done
shellcheck -x -P SCRIPTDIR ./*.sh tests/*.sh
python3 tests/test_media_normalization.py
bash tests/test_pipeline.sh
bash tests/test_startup.sh
bash tests/test_notifications.sh
/usr/bin/python3 tests/test_supervisor.py
bash tests/test_media.sh
