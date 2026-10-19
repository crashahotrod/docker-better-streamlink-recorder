# docker-better-streamlink-recorder

Record Twitch and Kick livestreams with Streamlink, with optional MP4 remuxing,
H.264 encoding, YouTube uploads, and Discord go-live notifications.

Published image: https://hub.docker.com/r/crashahotrod/docker-better-streamlink-recorder

## Setup

Copy `docker-compose.yml.sample` to `docker-compose.yml`, set the channel and
service credentials, and run `docker compose up -d`. To use local changes, build
with `docker build -t streamlink-recorder:local .` and set the service's `image`
to `streamlink-recorder:local`.

The image runs the recorder and workers as the `apps` user (UID/GID 568 by
default). Its root entrypoint creates the queue directories and assigns volume
ownership before starting the application. Existing scratch files are preserved.
Local `docker-compose.yml`, `.env` files and uploader credentials are excluded
from Git. The Docker build context includes runtime sources, security pins and
two platform-test helpers; local secrets, volumes and generated artifacts are excluded.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODE` | Required | `twitch` or `kick`, lowercase. |
| `CHANNEL` | Required | Channel slug, without URL or path separators. |
| `CLIENT_ID` | Required | Application client ID for the selected service's live-status API. |
| `CLIENT_SECRET` | Required | Corresponding application client secret. |
| `REMUX` | `false` | After capture, stream-copy TS to MP4. MP4 input is normalized as MP4 regardless of this flag. |
| `ENCODE` | `false` | Convert completed recordings to H.264/AAC MP4 using CRF 23 and the medium preset. |
| `UPLOAD` | `false` | Upload completed recordings to YouTube before optional encoding. |
| `UPLOAD_BOT_NAME` | Required for uploads | Name included in the upload description. |
| `YOUTUBE_PRIVACY` | `public` | Upload visibility: `public`, `private`, or `unlisted`. Use `private` for smoke tests. |
| `YOUTUBE_SPLITTING` | `false` | Limit each recording segment to 11h59m30s for YouTube. |
| `DISCORD_WEBHOOK_URL` | Unset | Optional go-live notification webhook. Notification failures do not stop recording. |
| `TWITCH_USER_TOKEN` | Unset | Optional user OAuth token for authenticated Twitch access, such as subscribed-channel ad handling. |

Boolean values must be exactly `true` or `false`. Twitch recording does not
require `TWITCH_USER_TOKEN`; anonymous operation omits its authorization header.
See [Streamlink's Twitch authentication instructions](https://streamlink.github.io/cli/plugins/twitch.html#authentication)
for obtaining a user token. Application client credentials are still required
for live-status checks.

## Processing behavior

Every recording first captures Streamlink's original bytes in `capture.bin`.
After capture completes, a normalization worker detects TS or MP4 from its contents,
checks every initialization period, and creates verified outputs. `REMUX=true`
now runs after capture, so an FFmpeg failure cannot interrupt the recorder.
MPEG-TS remains TS when `REMUX=false`; fragmented MP4 always becomes MP4.

Repeated compatible MP4 periods are joined only if frame content, sample counts,
and presentation timing survive. Codec/track changes, or boundaries that cannot
be joined without changing A/V timing, become stable ` - part-0001.mp4`,
` - part-0002.mp4` files. Repair uses stream copy; `ENCODE=true` explicitly requests
H.264/AAC encoding of those verified parts. Uncertain inputs stay in quarantine.

The processing route follows normalization:

| UPLOAD | ENCODE | Completed recording route |
| --- | --- | --- |
| `false` | `false` | Store verified TS or MP4 parts. |
| `false` | `true` | Encode to MP4, then store. |
| `true` | `false` | Upload each verified part, then store. |
| `true` | `true` | Upload, then encode to MP4, then store. |

Stored recordings use `<channel>/Season YYYYMM/` with Plex-style season and
episode names. Each filename includes a stream identifier and unique recording
suffix so segments and reconnects do not overwrite each other. MP4 files receive
title metadata; TS files retain their title in the filename. Validation checks decoding, video frame counts and identity, timing, audio coverage,
and MP4 sample accounting. Encoding must preserve the verified video frame count
and timing. The original capture is removed only after every part is published
and checksummed.
Audio is optional for streams that do not provide it.

## Volumes and credentials

| Container path | Purpose |
| --- | --- |
| `/etc/streamlink/scratch` | Persistent active recordings, pending jobs, retry state, and upload receipts. |
| `/storage` | Completed recordings. May be on a different filesystem from scratch. |
| `/config` | YouTube credentials, required only when `UPLOAD=true`. |

For uploads, provide both `/config/youtubeuploader_client_secrets.json` and
`/config/youtubeuploader_request.token`, readable by the application user. The
config volume must be writable because OAuth token refreshes can update it.
Generate the token interactively beforehand using the
[youtubeuploader setup instructions](https://github.com/porjo/youtubeuploader/tree/v1.25.5#setup).
The worker passes absolute credential paths rather than relying on its working
directory or `/etc` symlinks. Its `-metaJSONout` option preserves returned upload
metadata alongside the job; see the [uploader options](https://github.com/porjo/youtubeuploader/tree/v1.25.5#usage).

Publication copies to a temporary directory on the storage filesystem, verifies
the copy, applies MP4 metadata, and commits it with a hard link that refuses to
overwrite an existing filename. Storage must support hard links within one
directory. An unsupported mount or conflicting file leaves the scratch source
pending and logs a failure. Keep enough storage capacity for the temporary copy.

## Retries and recovery

New recordings use `/etc/streamlink/scratch/<mode>/<channel>/queue-v2/`:

- `active/`: in-progress or interrupted jobs. Only explicitly completed jobs can
  advance automatically; uncertain partial recordings stay here for inspection.
- `normalize/`: completed captures awaiting validation and stream-copy repair.
- `quarantine/`: uncertain or corrupt jobs retained for inspection, without automatic
  upload, encoding, publication, or deletion.
- `upload/`: verified parts awaiting upload or local upload handoff.
- `encode/`: recordings awaiting successful encoding and publication.
- `publish/`: recordings awaiting publication without encoding.

Each job directory contains its media, original title and filename metadata,
captured processing flags, and any retry or success state. Workers scan ready
queues on startup and every five seconds. A Linux file lock prevents overlapping
workers for the same stage. Failures retry with backoff from 30 seconds up to
15 minutes; other jobs continue to be considered. Individual uploader attempts
have a six-hour timeout. Inspect logs for the job path and failing stage.

Each `parts/0001.uploaded` receipt prevents repeating a successful part after a
local failure; other parts remain pending. A crash between remote success and
saving this receipt can still duplicate an upload. For an uncertain attempt,
stop the container and reconcile that part's `parts/0001.remote.json` with the
YouTube channel before resuming. A confirmed upload can be acknowledged with
`true` in that part's `.uploaded` file. Old job-wide `uploaded`/publication receipts
require manual reconciliation and are quarantined rather than re-uploaded.

`normalized.json` records capture and output checksums, source byte ranges,
track/codec settings, sample/frame counts, timestamps, and decoded-content hashes.
`validation.json` gives the current result; `diagnostics/` keeps command arguments,
exit codes, timing, and stderr. Decoder errors are checked even when FFmpeg exits
zero. Full validation decodes media and costs CPU; it does not re-encode it.

Reserve scratch space for the original capture, extracted periods, and candidates
(roughly three times the capture size, plus diagnostics and optional encoded
outputs). Decoded frame hashes are streamed to disk and condensed after success;
failed attempts retain their diagnostics. Publication needs another output-sized
temporary copy on storage. Failed repair never deletes the capture to free space.
Validation cost depends on resolution, frame rate, and available CPU. The
development benchmark below can measure overhead on generated recordings.

An interrupted publication is recognized from its saved checksum and final
file. Workers finish cleanup when the checksum matches. Incomplete temporary
outputs are never treated as ready jobs; encoding retries overwrite only that
job's temporary MP4. Successfully published jobs are first renamed to hidden
`.completed.*` directories so an interrupted cleanup is safely resumed. A killed process may leave `.publish.*` directories in
storage. With the container stopped, inspect and remove only those confirmed
abandoned temporary copies; retain the scratch source and final recording.

When changing `UPLOAD` or `ENCODE`, remember that each job retains the route
captured when recording began. Pending upload/encode work requires the relevant
worker to remain enabled until drained. Do not rewrite pending-job flags while
the container is running.

Before upgrading, preserve the scratch volume. Legacy `download/` and `encode/`
files are deliberately excluded from automatic processing because they lack a
reliable completion marker. With the container stopped, review those files and
copy usable recordings to storage or convert them separately. Do not delete the
originals until their stored copies have been verified.

For interrupted jobs in `queue-v2/active`, stop the recorder before inspection.
Only explicitly completed jobs may be resumed; never promote an active writer.
For a completed quarantined job without an existing `normalized.json`, run the
recovery tool in an offline container with the same volumes and environment:

```sh
docker compose stop streamlinkrecorder
docker compose run --rm --no-deps streamlinkrecorder bash /etc/streamlink/tools/recover.sh \
  /etc/streamlink/scratch/twitch/CHANNEL/queue-v2/quarantine/recording.JOB
```

Replace `streamlinkrecorder` with your Compose service name and use the actual job path.
The command writes new candidates, preserves `capture.bin` (or legacy `media.ts` /
`media.mp4`), and queues only a verified result. It refuses existing manifests and
legacy success receipts. A wrong init map, missing initialization, absolute fragment
data offsets, or repeated MOOV without an independent file-type boundary may need
the original playlist/init data or manual recovery. Keep the originals. For an
already normalized job, inspect its manifest, receipts, and diagnostics rather
than removing them to force a retry. An interrupted normalization can leave
`.normalizing.*` directories; inspect these only with the container stopped.

## Development checks

On Linux with Bash, Python 3, FFmpeg/ffprobe, ExifTool, jq, ShellCheck,
Supervisor (available to `/usr/bin/python3`), and `flock` installed, run
`bash tests/run.sh`. Tests use generated short media and
mock network/upload operations; they require no service credentials. CI runs
these checks separately from publishing the image. Live-service checks require
real credentials and are not part of the automated suite.
See [the test guide](tests/README.md) for feature coverage, container and permission
checks, and the integration checks that still need live services or target hardware.

Shell scripts use LF endings, enforced by `.gitattributes` for Windows checkouts.
Expected negative tests print retained-input, validation, and permission
errors; a passing suite ends in `PASS`/`OK`. CI saves failed media diagnostics.
`tests/test_media_normalization.py` covers MP4 periods, timing, completeness,
and retained captures. With Streamlink installed it also tests local HLS capture
through every processing route. Run the full opening regression with `TEST_MEDIA_INTRO_SECONDS=90 TEST_MEDIA_MAIN_SECONDS=6 python3 tests/test_media_normalization.py`.
The default uses a two-second opening for fast feedback. Run
`python3 tests/benchmark_media.py --output /tmp/new-benchmark.json` for the eight-hour
tiny-video benchmark. Tests never use live uploads or service credentials.

## Releases and dependency updates

The image uses the maintained Python 3.12 slim Bookworm base. The uploader is
built from its tagged upstream source using Go 1.27.2 and the security updates
pinned in `build/uploader-modules.txt`; its upstream tests run during the build.
Build tools stay in separate build stages. Keep persistent
scratch/storage volumes when upgrading; do not prune pending or quarantined jobs.

CI checks workflows, the complete amd64 suite, an offline dependency scan, and
ARM64/ARMv7 image smoke tests before publishing. Version tags such as `v1.0.0`
also publish `1.0.0` and `1.0`
image tags. Use a versioned tag or digest when you need a fixed deployed version.
This repository's `latest` tag is updated by the default release flow.

Dependency automation checks for stable upstream releases, verifies them against
the test suite, and automatically commits updates to the default branch when all
checks pass. Commits to the default branch trigger multi-architecture image builds
and publication. Configure `REPO_PAT` with repository contents write permission so
pushed commits trigger downstream publication workflows.
Image publication also requires the existing Docker Hub repository/token secrets.
Review the Go toolchain and uploader module security pins when updating dependencies.
The image scan blocks publication on known critical/high vulnerabilities with
available fixes; it does not certify the absence of other vulnerabilities.

Before promoting a release, confirm live Twitch/Kick capture and, when enabled,
a private YouTube upload with your credentials. Offline tests exercise local HLS
and mocked API responses; they cannot certify remote authentication, future
service changes, or player/browser behavior on every target device.

## License

Project source is available under the [MIT License](LICENSE). Bundled dependencies
retain their respective licenses. Uploader build provenance, module versions and
license notices are included at `/usr/share/doc/youtubeuploader` in the image.
