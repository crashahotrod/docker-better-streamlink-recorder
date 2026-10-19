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

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODE` | Required | `twitch` or `kick`, lowercase. |
| `CHANNEL` | Required | Channel slug, without URL or path separators. |
| `CLIENT_ID` | Required | Application client ID for the selected service's live-status API. |
| `CLIENT_SECRET` | Required | Corresponding application client secret. |
| `REMUX` | `false` | Remux to MP4 during recording. Otherwise preserve the raw transport stream as `.ts`. |
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

`REMUX` selects the recording container independently of the processing route:

| UPLOAD | ENCODE | Completed recording route |
| --- | --- | --- |
| `false` | `false` | Store the original TS or remuxed MP4. |
| `false` | `true` | Encode to MP4, then store. |
| `true` | `false` | Upload, then store the original TS or remuxed MP4. |
| `true` | `true` | Upload, then encode to MP4, then store. |

Stored recordings use `<channel>/Season YYYYMM/` with Plex-style season and
episode names. Each filename includes a stream identifier and unique recording
suffix so segments and reconnects do not overwrite each other. MP4 files receive
title metadata; TS files retain their title in the filename. Encoding verifies
that the output contains video, and only removes its source after publication.
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
- `upload/`: completed recordings awaiting upload or local upload handoff.
- `encode/`: recordings awaiting successful encoding and publication.
- `publish/`: recordings awaiting publication without encoding.

Each job directory contains its media, original title and filename metadata,
captured processing flags, and any retry or success state. Workers scan ready
queues on startup and every five seconds. A Linux file lock prevents overlapping
workers for the same stage. Failures retry with backoff from 30 seconds up to
15 minutes; other jobs continue to be considered. Individual uploader attempts
have a six-hour timeout. Inspect logs for the job path and failing stage.

An `uploaded` marker prevents another upload if a later local handoff fails.
A crash between remote upload success and saving this marker can still result
in a duplicate upload. For an uncertain attempt, stop the container, inspect the
job's `remote.json` and the YouTube channel, and reconcile the result before
restarting. Confirmed successful uploads can be acknowledged by creating an
`uploaded` file containing `true` in that specific job. Do not acknowledge an
upload without verifying its remote result.

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
Check media with `ffprobe` and repair a copy where needed. A deliberately
recovered, usable job can be completed by sourcing `pipeline.sh`, calling
`pipeline_init`, then calling `complete_recording` with that job's full path in
an offline container using the same volumes and environment. Never promote an
active writer or move a bare media file into a ready queue: ready work consists
of the entire job directory and its metadata.

## Development checks

On Linux with Bash, Python 3, FFmpeg/ffprobe, ExifTool, jq, ShellCheck,
Supervisor (available to `/usr/bin/python3`), and `flock` installed, run
`bash tests/run.sh`. Tests use generated short media and
mock network/upload operations; they require no service credentials. CI runs
these checks separately from publishing the image. Live-service checks require
real credentials and are not part of the automated suite.

Shell scripts use LF endings, enforced by `.gitattributes` for Windows checkouts.
See `PLAN.md` for implementation progress and any remaining verification limits.
