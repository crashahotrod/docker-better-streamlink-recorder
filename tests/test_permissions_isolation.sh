#!/bin/bash
# Run as root: the application user must not need access to the CI checkout.
set -euo pipefail
[[ "$EUID" -eq 0 ]] || { echo 'Run this check as root with TEST_APP_USER set.' >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PRIVATE_CHECKOUT=$(mktemp -d)
trap 'find "$PRIVATE_CHECKOUT" -depth -delete' EXIT
chmod 700 -- "$PRIVATE_CHECKOUT"
mkdir -p -- "$PRIVATE_CHECKOUT/repository/tests"
cp -- "$ROOT/entrypoint.sh" "$ROOT/pipeline.sh" "$ROOT/notifications.py" "$PRIVATE_CHECKOUT/repository/"
cp -- "$ROOT/validate_content.py" "$ROOT/content.py" "$ROOT/cloud_sync.py" "$PRIVATE_CHECKOUT/repository/"
cp -- "$ROOT/tests/test_permissions.sh" "$PRIVATE_CHECKOUT/repository/tests/"
if gosu "${TEST_APP_USER:?Set TEST_APP_USER to an existing non-root user}" \
    test -r "$PRIVATE_CHECKOUT/repository/pipeline.sh"; then
    echo 'Regression setup failed: the application user can read the private checkout.' >&2
    exit 1
fi
bash "$PRIVATE_CHECKOUT/repository/tests/test_permissions.sh"
printf 'PASS: permission test works without application-user access to the checkout\n'
