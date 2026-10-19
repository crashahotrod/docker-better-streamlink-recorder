# Test suite

Run `bash tests/run.sh` as a non-root user on Linux with Bash, Python 3,
FFmpeg/ffprobe (libx264), ExifTool, jq, ShellCheck, Supervisor, and util-linux.
Python discovery uses `/usr/bin/python3` so it can import the system Supervisor
package. The runner checks shell syntax and ShellCheck before behavioral tests.

| Behavior | Coverage |
| --- | --- |
| Twitch/Kick app tokens, cache expiry, HTTP/transport failures, offline/live decisions, recording arguments, optional Twitch authentication, segment-limit setting | `test_download.sh` |
| Required settings, boolean flags, upload visibility, missing credentials | `test_startup.sh` |
| Discord JSON escaping, failed notifications allowing recording to continue | `test_notifications.sh` |
| MP4 initialization periods, compatible joins, codec/track changes, audio-first inputs, audio offsets/settings, B-frames, variable timing, truncated/uncertain inputs, sample counts, complete manifest accounting, source/output checksums, CLI validation, error diagnostics | `test_media_normalization.py` |
| Real TS capture and metadata through all 16 service/remux/upload/encode routes, all visibility choices, VFR encoding, separate filesystems, denied destination writes, capture retention, interrupted encode/normalization and simulated disk-full recovery | `test_media.sh` |
| Quarantine at every consumer boundary, per-part upload/publication receipts, retries, lock exclusion, collisions, partial copies, changed final files, interrupted cleanup and offline recovery | `test_pipeline.sh` |
| Backoff deadlines/cap, invalid retry state, queue progress despite failures, one-pass workers, completion handoff recovery, duplicate-monitor exclusion, persisted flags, Unicode filename limits and recovery path boundaries | `test_queue.sh` |
| Worker enable/disable flags and process-group shutdown configuration | `test_supervisor.py` |
| Stable-release dependency selection, immutable commit pins and failure without partial updates | `test_dependency_updates.py` |
| Fresh/reused volumes, all queue directories, unreadable credentials as the application user, private checkout isolation | `test_permissions.sh`, `test_permissions_isolation.sh` |
| Built entrypoint, persisted captures, actual workers for both services and every upload/encode flag combination, image dependencies and full HLS processing matrix | `test_container.sh` |
| Uploader/Streamlink execution and complete codec-change repair/publication on each advertised architecture | `test_platform.sh` |
| Known critical/high dependency vulnerabilities with available fixes; public database download separated from network-isolated image analysis | `scan_image.sh` |

The permission checks run separately as root against an existing non-root user:

```sh
sudo TEST_APP_USER=recorder-test bash tests/test_permissions.sh
sudo TEST_APP_USER=recorder-test bash tests/test_permissions_isolation.sh
```

Build and check the image with `docker build -t streamlink-recorder:test .` followed
by `bash tests/test_container.sh`. The image runs the same behavioral suite using
`tests/run.sh --behavior-only`, without requiring ShellCheck inside the image.
Every test container has external networking disabled. Real Streamlink captures
local HLS from container loopback; uploads and live-service responses are mocked.
The HLS integration test is skipped when Streamlink is unavailable locally and is
run by the image suite using 90-second and six-second fixture periods.

Run Python discovery with `/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v`,
or invoke a test file directly. Configure media durations with
`TEST_MEDIA_INTRO_SECONDS` and `TEST_MEDIA_MAIN_SECONDS`. Generated fixtures and
test volumes are temporary. Set `TEST_ARTIFACT_DIR` to retain failed media/pipeline
diagnostics; CI uploads these automatically. Expected negative cases log errors
but must finish with passing assertions and exit zero.

Disk-full behavior is injected without filling a volume. The long-recording
benchmark remains an optional developer tool (`benchmark_media.py`), separate
from functional checks. Real-service authentication, remote upload results,
hours-long recording limits, target-specific browser behavior, player GUI
compatibility, and behavior on physical target devices require separate integration checks.
CI additionally builds and smoke-tests ARM64 and ARMv7 images under QEMU before
publication; the complete recording suite runs on amd64.

Run `bash tests/scan_image.sh` after building the image to repeat the CI dependency
scan. Only the database download needs external networking. The image scan has
networking disabled and retains `security.json` under `TEST_ARTIFACT_DIR` when set.
It uses Trivy 0.76.0 and excludes vulnerabilities without an available fix.
