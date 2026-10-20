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
default, customizable via `PUID` and `PGID`). Its root entrypoint creates the queue directories and assigns volume
ownership before starting the application. Existing scratch files are preserved.
Local `docker-compose.yml`, `.env` files and uploader credentials are excluded
from Git. The Docker build context includes runtime sources, security pins and
two platform-test helpers; local secrets, volumes and generated artifacts are excluded.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUID` | `568` | Optional runtime user ID for storage and scratch file ownership (e.g. `1000`). |
| `PGID` | `568` | Optional runtime group ID for storage and scratch file ownership (e.g. `1000`). |
| `MODE` | Required | `twitch` or `kick`, lowercase. |
| `CHANNEL` | Required | Channel slug, without URL or path separators. |
| `CLIENT_ID` | Required | Application client ID for the selected service's live-status API. |
| `CLIENT_SECRET` | Required | Corresponding application client secret. |
| `STREAM_QUALITY` | `best` | Stream quality string passed to Streamlink (e.g. `1080p60,720p60,best`). |
| `STREAMLINK_EXTRA_ARGS` | Unset | Extra arguments passed to Streamlink, with shell-style quoting (e.g. `--twitch-low-latency`). See recipes below. |
| `AUTO_RECOVER_INTERRUPTED` | `false` | Automatically recover orphaned unsplit captures and validate/recover stopped segmented sessions before the next live-status check. |
| `REMUX` | `false` | After capture, stream-copy TS to MP4. MP4 input is normalized as MP4 regardless of this flag. |
| `ENCODE` | `false` | Convert completed recordings to H.264/AAC MP4. |
| `ENCODE_ACCEL` | `auto` | `auto`, `cpu`, `nvenc`, `vaapi`, or `qsv`. Auto infers the backend from `ENCODE_VIDEO_CODEC`, using CPU by default. |
| `ENCODE_CRF` | `23` | CRF for `libx264`/`libx265` CPU encoding (0–51); hardware encoders use `ENCODE_QUALITY`. |
| `ENCODE_QUALITY` | `23` | Hardware quality: NVENC CQ / VAAPI QP (0–51), QSV ICQ (1–51). Scales differ between encoders; NVENC `0` selects automatic quality. |
| `ENCODE_PRESET` | Backend-specific | `medium` for x264/x265/QSV, `p4` for NVENC. Leave unset for VAAPI, which has no preset option. |
| `ENCODE_VIDEO_CODEC` | Backend-specific | Defaults to `libx264` on CPU or `h264_nvenc`, `h264_vaapi`, `h264_qsv` for the selected accelerator. An explicit codec must match `ENCODE_ACCEL` unless it is `auto`. |
| `ENCODE_DEVICE` | `/dev/dri/renderD128` | Passed-through DRM render node used by VAAPI/QSV; unused for CPU/NVENC. |
| `ENCODE_AUDIO_BITRATE` | `128k` | Audio bitrate for AAC encoding. |
| `ENCODE_EXTRA_ARGS` | Unset | Extra arguments passed to FFmpeg during encoding, with shell-style quoting. |
| `UPLOAD` | `false` | Upload completed recordings to YouTube before optional encoding. |
| `UPLOAD_BOT_NAME` | Required for uploads | Name included in the upload description. |
| `YOUTUBE_PRIVACY` | `public` | Upload visibility: `public`, `private`, or `unlisted`. Use `private` for smoke tests. |
| `YOUTUBE_UPLOAD_THUMBNAIL` | `true` | Extract and set custom video thumbnail on upload (set `false` for unverified YouTube channels). |
| `YOUTUBE_SPLITTING` | `false` | Continuously record and publish closed segments while the broadcast continues, using downstream stream-copy splitting. |
| `YOUTUBE_SEGMENT_SECONDS` | `21600` | Target segment duration (6 hours). Integer from `1` to `42900` (11h55m), allowing room for keyframe-aligned cuts. Used when `YOUTUBE_SPLITTING=true`. |
| `YOUTUBE_PLAYLIST_ID` | Unset | Optional YouTube playlist ID to append uploaded videos to. |
| `YOUTUBE_TAGS` | Unset | Optional comma-separated tags for uploaded videos. Supports template tokens (e.g. `{channel}`, `{mode}`, `{part}`). |
| `YOUTUBE_CATEGORY_ID` | Unset | Optional YouTube numeric category ID (e.g. `20` for Gaming). |
| `YOUTUBE_DESCRIPTION` | Unset | Custom description template (defaults to `Uploaded Automatically by <UPLOAD_BOT_NAME>`). Supports placeholders `{channel}`, `{mode}`, `{title}`, `{date}`, `{part_title}`, `{part}`, `{count}`, `{stream_url}`, `{bot_name}`. |
| `YOUTUBE_THUMBNAIL_TIMESTAMP` | `00:01:00` | Offset (HH:MM:SS) to extract a video frame via FFmpeg as the YouTube video thumbnail. |
| `MIN_FREE_SPACE_GB` | `0` | Minimum free disk space in GB required on scratch/storage to start recording (`0` disables check). |
| `RETENTION_DAYS` | `0` | Prune expired MP4/TS/MKV recordings and their matching sidecars from storage (`0` disables pruning). |
| `QUARANTINE_RETENTION_DAYS` | `7` | Permanently delete whole quarantined jobs N days after entering quarantine (`0` keeps them indefinitely). Runs every 30 seconds, independently of publication. Legacy jobs use their directory modification time. |
| `POST_PUBLISH_COMMAND` | Unset | Custom shell command evaluated when a recording part is published. |
| `DISCORD_WEBHOOK_URL` | Unset | Optional Discord webhook for rich embed go-live and upload-complete alerts. Notification failures do not stop recording. |
| `DISCORD_MONITOR_WEBHOOK_URL` | Unset | Dedicated webhook for the persistent status message and operational alerts. Only operational alerts fall back to `DISCORD_WEBHOOK_URL`. |
| `DISCORD_NOTIFY_ON_ERROR` | `true` | Queue operational alerts, including recording completion, disk warnings, quarantine, and quota errors. Does not disable a dedicated persistent status message. |
| `DISCORD_USERNAME` | `Streamlink (<CHANNEL>)` | Optional username attribution for Discord webhook notifications. |
| `DISCORD_AVATAR_URL` | Unset | Optional avatar image URL for Discord webhook notifications. |
| `RECORDER_STALE_SECONDS` | `180` | Report an error after this many seconds without monitor progress or capture writes. Must be at least `150` to allow bounded API requests. |
| `TWITCH_USER_TOKEN` | Unset | Optional user OAuth token for authenticated Twitch access, such as subscribed-channel ad handling. |

Boolean values must be exactly `true` or `false`. Numeric values (`ENCODE_CRF`, `MIN_FREE_SPACE_GB`, `RETENTION_DAYS`, `QUARANTINE_RETENTION_DAYS`)
must be positive integers (or `0` to disable). Twitch recording does not
require `TWITCH_USER_TOKEN`; anonymous operation omits its authorization header.
See [Streamlink's Twitch authentication instructions](https://streamlink.github.io/cli/plugins/twitch.html#authentication)
for obtaining a user token. Application client credentials are still required
for live-status checks.

When `POST_PUBLISH_COMMAND` is specified, the command is executed in a subshell with
`$PUBLISHED_FILE` (absolute path of published file), `$PUBLISHED_CHANNEL`, and `$PUBLISHED_MODE`
exported in its environment.

When `DISCORD_WEBHOOK_URL` is set, rich embeds are sent on stream start and upon YouTube upload
completion (including direct links to uploaded videos). If YouTube reports an API quota or daily video-count limit,
upload workers detect `quotaExceeded` or `uploadLimitExceeded` and back off until the next
midnight in `America/Los_Angeles`, including daylight saving changes. Uploaders using
the same YouTube application client ID share a persistent cooldown and upload lock
when they share the scratch volume, even if their channel or config paths differ.
Independent scratch volumes cannot share this coordination state.

Notifications are written atomically to a persistent outbox and delivered by a
separate worker. Recording and upload workers never wait for Discord HTTP requests.
Successful uploads save their per-part receipt before notification work begins.

### Extra arguments and Streamlink options

`STREAMLINK_EXTRA_ARGS` and `ENCODE_EXTRA_ARGS` use Python's `shlex.split` with
POSIX shell-style quoting. Double/single quotes group spaces into one argument;
the grouping quotes are removed. Escaped spaces, quoted empty arguments and
newlines are preserved. Unmatched quotes or a trailing escape fail the operation
before Streamlink/FFmpeg starts. No shell expansion, command substitution or glob
expansion is performed. Docker Compose still applies its own variable
interpolation; use `$$` when passing a literal dollar sign through Compose.

For example, these Compose environment entries pass each header, filter and
metadata value as one argument:

```yaml
environment:
  - STREAMLINK_EXTRA_ARGS=--http-header "X-Recorder=archive worker" --stream-segment-threads 2
  - ENCODE_EXTRA_ARGS=-vf "scale=1280:-2,setsar=1" -metadata "comment=Recorded automatically"
```

Optional Streamlink recipes for the bundled version:

| Use | `STREAMLINK_EXTRA_ARGS` |
| --- | --- |
| Twitch low latency | `--twitch-low-latency --stream-segment-threads 2` |
| Kick low latency | `--kick-low-latency --stream-segment-threads 2` |
| Start at the earliest available HLS playlist segment | `--hls-live-restart --stream-segment-threads 2` |

Low latency is optional and can increase buffering; the broadcaster must support
it. See the [Streamlink options](https://streamlink.github.io/latest/cli.html#plugin-specific-options).
The bundled Streamlink accepts `--twitch-disable-reruns` and `--twitch-disable-ads`
as hidden compatibility switches, but they have no effect. It has no active
rerun-filter setting, and Twitch ad filtering is automatic. Filtering pauses capture during the
ad break rather than replacing it with broadcast content; see
[Twitch embedded ads](https://streamlink.github.io/cli/plugins/twitch.html#embedded-ads).

### Hardware encoding

Set `ENCODE=true` and choose `ENCODE_ACCEL=nvenc`, `vaapi`, or `qsv`. Existing
`ENCODE_VIDEO_CODEC=h264_nvenc`, `h264_vaapi`, or `h264_qsv` settings also work
with the default `ENCODE_ACCEL=auto`. CPU encoding remains the default.

| Backend | Quality options generated | Default preset | Device setup |
| --- | --- | --- | --- |
| CPU x264/x265 | `-crf 23` | `medium` | None |
| NVIDIA NVENC | `-rc:v vbr -cq:v 23 -b:v 0` | `p4` | NVIDIA driver and NVIDIA Container Toolkit on the Docker host; GPU reservation below |
| VAAPI (Intel/AMD) | `-rc_mode:v CQP -qp:v 23` | None | Render node passed through with `devices` |
| Intel QSV | `-global_quality:v 23` | `medium` | Render node passed through with `devices`; QSV device initialized before input |

Change hardware quality with `ENCODE_QUALITY`; lower nonzero values generally
favor quality, but the same value is not equivalent across backends. NVENC accepts
presets `p1`–`p7`; QSV accepts `veryfast` through `veryslow`. CPU-only CRF/preset
flags are not injected into other custom CPU codecs; supply their options via
`ENCODE_EXTRA_ARGS`. See [FFmpeg codec options](https://ffmpeg.org/ffmpeg-codecs.html)
and [NVIDIA's FFmpeg guide](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html).

For VAAPI or QSV on a Linux Docker host:

```yaml
environment:
  - ENCODE=true
  - ENCODE_ACCEL=vaapi # or qsv
  - ENCODE_QUALITY=23
  - ENCODE_DEVICE=/dev/dri/renderD128
devices:
  - /dev/dri/renderD128:/dev/dri/renderD128
```

The root entrypoint adds the application user to the render node's group before
switching users; it does not change device ownership or permissions on the host.
Docker `group_add` alone is insufficient because `gosu` rebuilds supplementary
groups. The image includes Mesa VAAPI drivers and, on amd64, Intel iHD/i965
drivers. The host must provide a supported GPU, kernel driver, firmware and
render node. Select the appropriate node when multiple GPUs are present.
Debian's free Intel driver has a limited encoder set; some GPU generations need
additional vendor media drivers/shaders in a custom image. Check the
[Intel driver package's supported encoders](https://packages.debian.org/bookworm/intel-media-va-driver).

VAAPI defaults to `-vf "format=nv12,hwupload"` with software decoding. If you
provide a custom video filter/complex graph in `ENCODE_EXTRA_ARGS`, it replaces
that default and must perform its own hardware upload, for example:
`-vf "scale=1280:-2,format=nv12,hwupload"`. Avoid CPU-only `ENCODE_PRESET` values
with VAAPI; leave it unset. See [FFmpeg hardware device setup](https://ffmpeg.org/ffmpeg.html#Advanced-Video-options).

For NVENC, install NVIDIA Container Toolkit on the host and request a GPU:

```yaml
environment:
  - ENCODE=true
  - ENCODE_ACCEL=nvenc
  - ENCODE_QUALITY=23
  - ENCODE_PRESET=p4
  - NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

Hardware encoding does not enable GPU decoding or change the upload route.
See [Docker's GPU reservation setup](https://docs.docker.com/compose/how-tos/gpu-support/)
for host configuration.
Normalization and validation still decode on the CPU. Every encoded part must
pass the existing frame/timing/audio and publication checks. Hardware/driver
failures retain the job for retry; there is no automatic CPU fallback.

### Storage retention and sidecars

After successful publication, `RETENTION_DAYS` prunes expired `.mp4`, `.ts`, and
`.mkv` files under the current channel's storage directory. Age keeps the existing
`find -mtime +N` whole-day rounding (more than N complete 24-hour periods).
When an expired media file is deleted, its literal `stem.*` companions are also
removed, regardless of sidecar age: for example `episode.nfo`, `episode.json`,
`episode.jpg`, and `episode.en.srt`. Empty season directories are then removed.

Sidecars remain when another retained media version shares that basename. The
most specific media stem owns a sidecar, so expiring `episode.mp4` preserves
`episode.part1.jpg` when `episode.part1.mp4` remains. Unrelated metadata, symlinks,
other channel directories and temporary `.publish.*` directories are preserved.
Pruning uses a per-channel `.retention.lock` in storage to serialize workers.

## Processing behavior

Every recording first captures Streamlink's original bytes in `capture.bin`.
For segmented recordings, that original lives under `sessions/` as described below.
After capture completes, a normalization worker detects TS or MP4 from its contents,
checks every initialization period, and creates verified outputs. `REMUX=true`
now runs after capture, so an FFmpeg failure cannot interrupt the recorder.
MPEG-TS remains TS when `REMUX=false`; fragmented MP4 always becomes MP4.

If recording exits before writing any media bytes, its empty job and active
pointer are removed, even if Streamlink exits successfully. Empty segmented
sessions also remove their child jobs and capture lock after writers stop.
Nonempty interrupted captures remain available for recovery.

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

### Continuous YouTube segmentation

With `YOUTUBE_SPLITTING=true`, one Streamlink CLI session writes continuous stdout
to a disk-backed relay. The relay preserves the original bytes in
`queue-v2/sessions/recording.<parent_id>/capture.bin`. Downstream FFmpeg uses
`-c copy` to create keyframe-aligned segments while capture continues. Segmentation
never enables encoding. A new MP4 initialization period starts a new FFmpeg input
epoch, preserving codec/init changes without reconnecting Streamlink.

The target is `YOUTUBE_SEGMENT_SECONDS` (six hours by default). Actual cuts follow
source keyframes. Closed files must pass the 11h59m30s duration ceiling and a
conservative 240 GB size ceiling before they can advance; normalization checks
decoded presentation duration as well. Unsafe or oversized results retain their
original source for inspection. Upload completion still depends on validation,
network bandwidth, and API quota.

Child jobs are named `recording.<parent_id>.seg<N>`. Each carries `segment-index`,
`segment-parent`, and `segment-epoch` metadata. Completed children enter the usual
normalize/upload/encode/publish queues immediately, and `active.job` follows the
currently growing child's `capture.bin`. Status duration and size describe that
child. Filenames and upload titles include `part-0001`, `part-0002`, and so on;
an additional normalization part uses `subpart-0001` to avoid collisions. Long
upload titles are shortened while preserving their segment suffix.

TS stays TS with `REMUX=false`; `REMUX=true` and MP4 inputs produce finalized MP4
directly in the segmenter. Normalization validates and copies these finalized
files without another remux. `ENCODE=true` retains its existing explicit encoding
stage after upload. Segment handoffs produce pipeline logs; the Discord
**Recording Completed** alert is sent once when the continuous broadcast capture
finishes successfully, using the full broadcast's captured size and duration.

Scratch space is checked before capture, continuously while draining stdout, and
at rotations. Segmented capture reserves the larger of `MIN_FREE_SPACE_GB` and
1 GiB, plus twice the growing segment's size for processing. Normalization also
waits for this headroom before creating candidates. If the reserve is reached,
capture stops safely, queues a disk alert, and retains the original and unfinished
children. This safety floor applies even when `MIN_FREE_SPACE_GB=0`.

Reserve space for the **original entire broadcast**, the current segment, and
normalization/encoding candidates. After the broadcast finishes and every child
is checksummed in storage, a separate audit worker checks source packet counts
and the exact decoded video-frame sequence across segment boundaries before
removing the session's original bytes. This adds a validation decode, not an
encode, and runs independently of normalization/publication workers. An
`audit-failed` or `interrupted` session stays in `sessions/` for manual
review. Preserve it during upgrades. Scratch must support hard links between
session outputs and child jobs on that same filesystem.

By default, only explicitly sealed children resume automatically. Recover a stopped
segmented session with `recover.sh` as described below, or opt into
`AUTO_RECOVER_INTERRUPTED=true`. Recovery validates closed output without another
remux. If a muxer trailer or closed output is missing, it replays the retained raw
source through the same downstream stream-copy segmenter, using the saved target
duration. Older sessions use the current `YOUTUBE_SEGMENT_SECONDS`; retain the
original setting when recovering them. The raw source is read directly without
making a second broadcast-sized copy. Rebuilt acknowledged segments are checked
and discarded; only missing children enter the queue.

Recovery acquires writer and worker locks and verifies complete source video-frame
coverage and audio/video packet counts before promoting any candidates. A live
writer, insufficient scratch space, corrupt source, or identity mismatch leaves
the original and existing jobs intact. Recovery can take time on long broadcasts;
reserve space for missing segments and validation candidates. Interrupted
broadcasts produce an **Interrupted recording recovered** alert, separately from
the successful broadcast's **Recording Completed** alert. After publication,
recovered sessions use the normal source audit and retirement process.

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
  upload, encoding, or publication. Deleted after `QUARANTINE_RETENTION_DAYS` (default seven days).
- `upload/`: verified parts awaiting upload or local upload handoff.
- `encode/`: recordings awaiting successful encoding and publication.
- `publish/`: recordings awaiting publication without encoding.
- `notifications/`: queued Discord events, independent of recording jobs and their cleanup.
- `sessions/`: segmented-broadcast metadata, original bytes, diagnostics, and child publication receipts.

Shared `youtube-quota/` and `discord-state/` directories live directly under the
scratch root. Preserve these along with channel queues when upgrading.

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
For an interrupted segmented session, pass the parent `sessions/recording.JOB`
directory to the same offline tool; no manual completion marker is needed:

```sh
docker compose stop streamlinkrecorder
docker compose run --rm --no-deps streamlinkrecorder bash /etc/streamlink/tools/recover.sh \
  /etc/streamlink/scratch/twitch/CHANNEL/queue-v2/sessions/recording.JOB
docker compose up -d streamlinkrecorder
```

This preserves existing upload/publication receipts and part numbers. Repeating
the command after successful recovery does not queue those segments again.
Truncated or corrupt source bytes that cannot pass full validation still require
manual repair; the command retains them. Parent session originals are never
removed by quarantine retention.

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

## Status monitoring and Discord alerts

### JSON status exporter (`status.json`)

The container continuously writes an atomic status manifest to
`/etc/streamlink/scratch/status-<CHANNEL>-<MODE>.json` (and `<work_dir>/status.json`) for external dashboards
(such as Home Assistant, Uptime Kuma, or monitoring scripts).

Each channel/mode has its own root manifest when containers share scratch.
Update dashboards that previously read the shared root `status.json` path.

```json
{
  "channel": "streamer_name",
  "mode": "twitch",
  "status": "recording",
  "recorder_heartbeat_at": 1792390500,
  "updated_at": "2026-10-19T06:15:30Z",
  "active_capture": {
    "job_id": "recording.XXXXXXXX",
    "title": "Stream title",
    "author": "streamer_name",
    "started_at": 1792386930,
    "duration_seconds": 3600,
    "duration_human": "01:00:00",
    "size_bytes": 1073741824,
    "size_human": "1.00 GB",
    "last_growth_at": 1792390530,
    "stalled": false,
    "segment_index": 2,
    "total_duration_seconds": 7200,
    "total_duration_human": "02:00:00"
  },
  "queues": {
    "active": 1,
    "normalize": 0,
    "upload": 0,
    "encode": 0,
    "publish": 0,
    "quarantine": 0
  },
  "disk_space": {
    "scratch_free_gb": 120,
    "storage_free_gb": 450,
    "min_free_space_gb": 5
  }
}
```

- `status`: `offline` (confirmed offline stream or stopped recorder), `monitoring` (checking the stream API), `recording` (active capture with recent writes), or `error` (API/capture failure, stale monitor heartbeat, or stalled capture). A stale state file alone cannot report a running recorder.
- `recorder_heartbeat_at`: Last monitor progress timestamp. During a long capture, recent file writes establish recording activity instead of the monitor heartbeat.
- `active_capture`: Details of an active or stalled recording, or `null` when the recorder is stopped or has no active job. `last_growth_at` is the capture's last write time (the job creation time before the first bytes arrive); `stalled` indicates no writes for `RECORDER_STALE_SECONDS`. During continuous YouTube segmentation (`YOUTUBE_SPLITTING=true`), `segment_index` tracks the 1-based child segment number, and `total_duration_seconds` / `total_duration_human` track the total broadcast elapsed time.
- `queues`: Real-time job counts across all pipeline stages.
- `disk_space`: Available gigabytes on the scratch and storage filesystems.

The Docker healthcheck requires the download, normalization, publication, status,
notification and source-audit workers, plus upload/encode workers enabled by their flags, to
be `RUNNING`. Disabled workers are excluded. Status export runs independently of
Discord delivery, so slow webhooks cannot stop local status updates.

### Discord monitor webhook and alerts

Configure notifications using webhook URLs in your environment:

- `DISCORD_WEBHOOK_URL`: Primary webhook for public stream go-live and YouTube upload success notifications.
- `DISCORD_MONITOR_WEBHOOK_URL`: Dedicated monitor webhook for administrative status and operational alerts. If unset, operational alerts fall back to `DISCORD_WEBHOOK_URL`.
- `DISCORD_NOTIFY_ON_ERROR`: Broadcast failure and operational events (default `true`).

#### Persistent status message

When `DISCORD_MONITOR_WEBHOOK_URL` is configured, the recorder creates a persistent status embed displaying the current channel status, active recording statistics, queue counts, and storage space.
The message ID is persisted in `$WORK_DIR/status_message.id`, with webhook identity
and retry/update state in `$WORK_DIR/discord-status.json`. The notification worker
updates it as data changes or on a 30-second interval. If the message is deleted
in Discord (HTTP 404), the worker creates a new message. Changing the monitor
webhook creates a status message at the new destination. A primary webhook alone
never enables the persistent message; it receives public events and, when enabled,
operational-alert fallback.

Payloads disable mentions and truncate text to Discord's individual and combined
embed limits. HTTP 429 responses defer requests using Discord's `Retry-After` and
`retry_after` values, with cooldown state shared by webhook across the scratch
volume. Other event delivery failures retry with backoff from 30 seconds to
15 minutes. Pending events survive worker/container restarts. Delivery is at least
once: a crash after Discord accepts an event but before local cleanup can duplicate
that alert. Failed events remain in the outbox until delivered or manually removed;
malformed events are retained as `.invalid` files for inspection.

#### Operational event alerts

Critical events are posted as distinct, separate messages:

- **Recording completed**: Stream title, duration, and captured size when a recording finishes.
- **Low disk space**: Warning when available disk space drops below `MIN_FREE_SPACE_GB` (debounced to once every 5 minutes).
- **Job quarantined**: Alert when a capture fails normalization or integrity verification and requires manual review.
- **YouTube quota exceeded**: Notification when YouTube reports an API quota or upload-count limit, indicating the next Pacific midnight using a Discord timestamp displayed in each viewer's timezone.

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
