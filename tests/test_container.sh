#!/bin/bash
# Requires a locally built image and a running Docker engine; no live services.
set -euo pipefail
IMAGE="${TEST_IMAGE:-streamlink-recorder:test}"
SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d)
container_id=''
TEST_MOUNT_ROOT="$TEST_ROOT"
if command -v cygpath > /dev/null; then
    TEST_MOUNT_ROOT=$(cygpath -m "$TEST_ROOT")
    SOURCE_ROOT=$(cygpath -m "$SOURCE_ROOT")
    export MSYS_NO_PATHCONV=1
fi
cleanup() {
    if [[ -n "$container_id" ]]; then docker rm -f "$container_id" > /dev/null 2>&1 || true; fi
    # The image's app user owns queue jobs; use root in the same image to clean
    # only the isolated test mount before deleting its empty host directory.
    docker run --rm --entrypoint bash \
        --mount "type=bind,source=$TEST_MOUNT_ROOT,target=/test-cleanup" \
        "$IMAGE" -c 'find /test-cleanup -mindepth 1 -depth -delete'
    rmdir -- "$TEST_ROOT"
}
trap cleanup EXIT
mkdir -- "$TEST_ROOT/scratch" "$TEST_ROOT/storage" "$TEST_ROOT/config"
touch "$TEST_ROOT/config/youtubeuploader_client_secrets.json" "$TEST_ROOT/config/youtubeuploader_request.token"
for upload in false true; do
    for encode in false true; do
        docker run -i --rm -e MODE=twitch -e CHANNEL=test -e CLIENT_ID=example \
            -e CLIENT_SECRET=example -e "UPLOAD=$upload" -e "ENCODE=$encode" \
            -e UPLOAD_BOT_NAME=smoke-test -e YOUTUBE_PRIVACY=private \
            --mount "type=bind,source=$TEST_MOUNT_ROOT/scratch,target=/etc/streamlink/scratch" \
            --mount "type=bind,source=$TEST_MOUNT_ROOT/storage,target=/storage" \
            --mount "type=bind,source=$TEST_MOUNT_ROOT/config,target=/config" \
            "$IMAGE" bash -s <<'SH'
set -euo pipefail
[[ $(id -u) -ne 0 ]]
python3 -c 'import streamlink.webbrowser.chromium'
streamlink --version > /dev/null
source /etc/streamlink/tools/pipeline.sh
pipeline_init
new_recording_job smoke test 1
printf 'preserve across restart' > "$RECORDING_JOB/media.ts"
for stage in active upload encode publish; do [[ -w "$WORK_DIR/$stage" ]]; done
/usr/bin/python3 - <<'PY'
import os
from supervisor.options import ServerOptions
options = ServerOptions()
options.realize(args=['-c', '/etc/supervisor/conf.d/supervisord.conf'])
programs = {p.name: p.autostart for g in options.process_group_configs for p in g.process_configs}
assert programs['streamlink_upload'] == (os.environ['UPLOAD'] == 'true')
assert programs['streamlink_encode'] == (os.environ['ENCODE'] == 'true')
assert programs['streamlink_download'] and programs['streamlink_publish']
PY
SH
    done
done
count=$(docker run --rm --entrypoint bash \
    --mount "type=bind,source=$TEST_MOUNT_ROOT/scratch,target=/test-scratch" \
    "$IMAGE" -c 'find /test-scratch -name media.ts | wc -l')
[[ "$count" -eq 4 ]]
# Start the real supervisor and workers with offline API responses.
mkdir -- "$TEST_ROOT/mocks"
cat > "$TEST_ROOT/mocks/curl" <<'SH'
#!/bin/bash
output=''
while (($#)); do
    if [[ "$1" == -o ]]; then output="$2"; fi
    shift
done
if [[ -n "$output" ]]; then
    printf '{"data":[]}\n' > "$output"
    printf '200'
else
    printf '{"access_token":"offline-test","expires_in":3600}\n'
fi
SH
chmod +x -- "$TEST_ROOT/mocks/curl"
container_id=$(docker run -d --rm -e MODE=twitch -e CHANNEL=test \
    -e CLIENT_ID=example -e CLIENT_SECRET=example -e UPLOAD=true -e ENCODE=true \
    -e UPLOAD_BOT_NAME=smoke-test -e YOUTUBE_PRIVACY=private \
    -e 'PATH=/mockbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    --mount "type=bind,source=$TEST_MOUNT_ROOT/mocks,target=/mockbin,readonly" \
    --mount "type=bind,source=$TEST_MOUNT_ROOT/scratch,target=/etc/streamlink/scratch" \
    --mount "type=bind,source=$TEST_MOUNT_ROOT/storage,target=/storage" \
    --mount "type=bind,source=$TEST_MOUNT_ROOT/config,target=/config" \
    --mount "type=bind,source=$SOURCE_ROOT,target=/project,readonly" "$IMAGE")
ready=false
for ((i=0; i<20; i++)); do
    if docker exec "$container_id" /usr/bin/python3 /project/tests/check_processes.py 2>/dev/null; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then docker logs "$container_id"; exit 1; fi
docker stop --time 10 "$container_id" > /dev/null
container_id=''
# Run media and recovery checks with the actual image's Debian tools as apps.
docker run --rm --entrypoint bash --user apps \
    --mount "type=bind,source=$SOURCE_ROOT,target=/project,readonly" \
    --workdir /project "$IMAGE" -c '
        set -e
        bash tests/test_pipeline.sh
        bash tests/test_startup.sh
        bash tests/test_notifications.sh
        /usr/bin/python3 tests/test_supervisor.py
        bash tests/test_media.sh
    '
printf 'PASS: container entrypoint, app permissions, flag changes, persisted media\n'
