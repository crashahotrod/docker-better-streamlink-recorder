#!/bin/bash
# Download a public database separately; analyze the private image offline.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE="${TEST_IMAGE:-streamlink-recorder:test}"
SCANNER=aquasec/trivy:0.76.0
SCAN_ROOT=$(mktemp -d)
CACHE_VOLUME="recorder-scan-cache-${SCAN_ROOT##*/}"
IMAGE_VOLUME="recorder-scan-image-${SCAN_ROOT##*/}"
MOUNT_ROOT="$SCAN_ROOT"
if command -v cygpath > /dev/null; then
    MOUNT_ROOT=$(cygpath -m "$SCAN_ROOT")
    export MSYS_NO_PATHCONV=1
fi
cleanup() {
    local status=$?
    if [[ -f "$SCAN_ROOT/report.json" && -n "${TEST_ARTIFACT_DIR:-}" ]]; then
        mkdir -p -- "$TEST_ARTIFACT_DIR"
        cp -- "$SCAN_ROOT/report.json" "$TEST_ARTIFACT_DIR/security.json"
    fi
    docker volume rm "$CACHE_VOLUME" "$IMAGE_VOLUME" > /dev/null || true
    rm -f -- "$SCAN_ROOT/report.json"
    rmdir -- "$SCAN_ROOT"
    exit "$status"
}
trap cleanup EXIT
docker volume create "$CACHE_VOLUME" > /dev/null
docker volume create "$IMAGE_VOLUME" > /dev/null
# This container has no access to image contents or project files.
docker run --rm -e TRIVY_SKIP_VERSION_CHECK=true \
    --mount "type=volume,source=$CACHE_VOLUME,target=/cache" \
    "$SCANNER" image --cache-dir /cache --download-db-only --no-progress
docker image save "$IMAGE" | docker run --rm --network none -i --entrypoint sh \
    --mount "type=volume,source=$IMAGE_VOLUME,target=/input" \
    "$SCANNER" -c 'cat > /input/image.tar'
scan_status=0
docker run --rm --network none -e TRIVY_SKIP_VERSION_CHECK=true \
    --mount "type=bind,source=$MOUNT_ROOT,target=/scan" \
    --mount "type=volume,source=$CACHE_VOLUME,target=/cache" \
    --mount "type=volume,source=$IMAGE_VOLUME,target=/input,readonly" \
    "$SCANNER" image --cache-dir /cache --skip-db-update --skip-java-db-update \
    --offline-scan --scanners vuln --timeout 15m --severity CRITICAL,HIGH --ignore-unfixed \
    --exit-code 1 --format json --output /scan/report.json --input /input/image.tar || scan_status=$?
if (( scan_status != 0 )); then
    if [[ -s "$SCAN_ROOT/report.json" ]]; then
        python3 "$ROOT/tests/security_summary.py" "$SCAN_ROOT/report.json" || true
    else
        printf 'ERROR: Security scanner failed without producing a JSON report.\n' >&2
    fi
    if [[ -n "${TEST_ARTIFACT_DIR:-}" ]]; then
        printf 'Full scan report (when available): %s/security.json\n' "$TEST_ARTIFACT_DIR" >&2
    fi
    exit "$scan_status"
fi
printf 'PASS: no fixable critical/high dependency findings in release image\n'
