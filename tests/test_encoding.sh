#!/bin/bash
# Hardware argument/routing checks run without pretending that a GPU is present.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../encode.sh
source "$ROOT/encode.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
job="$TEST_ROOT/recording.encoder"
mkdir -p "$job/parts" "$job/diagnostics"
printf 'original capture' > "$job/capture.bin"
ensure_normalized_job() { return 0; }
part_ids() { printf '0001\n'; }
part_file() { printf '%s/parts/0001.mp4\n' "$1"; }
part_committed() { return 1; }
verify_part() { touch "$TEST_ROOT/verified"; }
publish_part() { touch "$TEST_ROOT/published"; }
remove_completed_job() { return 0; }
prune_old_recordings() { return 0; }
ffmpeg() {
    printf '%s\0' "$@" > "$TEST_ROOT/args"
    printf 'call\n' >> "$TEST_ROOT/calls"
    if [[ "${FAIL_ENCODER:-false}" == true ]]; then echo 'GPU unavailable' >&2; return 1; fi
}
get_args() { mapfile -d '' -t args < "$TEST_ROOT/args"; }
assert_option() {
    local i
    for i in "${!args[@]}"; do
        if [[ "${args[i]}" == "$1" ]]; then [[ "${args[i+1]}" == "$2" ]]; return; fi
    done
    echo "Missing option $1" >&2; return 1
}
assert_no_option() {
    local arg
    for arg in "${args[@]}"; do [[ "$arg" != "$1" ]] || { echo "Unexpected option $1" >&2; return 1; }; done
}
assert_before_input() {
    local arg
    for arg in "${args[@]}"; do
        [[ "$arg" != -i ]] || { echo "$1 must precede input" >&2; return 1; }
        [[ "$arg" != "$1" ]] || return 0
    done
    return 1
}
unset ENCODE_ACCEL ENCODE_VIDEO_CODEC ENCODE_PRESET ENCODE_CRF ENCODE_QUALITY ENCODE_DEVICE ENCODE_EXTRA_ARGS
encode_job "$job"
get_args
assert_option -c:v libx264
assert_option -crf 23
assert_option -preset medium
for backend in nvenc vaapi qsv; do
    ENCODE_ACCEL="$backend" ENCODE_QUALITY=27 encode_job "$job"
    get_args
    assert_option -c:v "h264_$backend"
    assert_no_option -crf
    assert_option -c:a aac
    assert_option -fps_mode passthrough
    case "$backend" in
        nvenc)
            assert_option -cq:v 27
            assert_option -rc:v vbr
            assert_option -b:v 0
            assert_option -preset p4
            assert_no_option -vaapi_device
            ;;
        vaapi)
            assert_option -qp:v 27
            assert_option -rc_mode:v CQP
            assert_option -vaapi_device /dev/dri/renderD128
            assert_before_input -vaapi_device
            assert_option -vf 'format=nv12,hwupload'
            assert_no_option -preset
            ;;
        qsv)
            assert_option -global_quality:v 27
            assert_option -preset medium
            assert_option -init_hw_device 'qsv=recorder:hw,child_device=/dev/dri/renderD128'
            assert_before_input -init_hw_device
            ;;
    esac
done
# Existing codec overrides infer the backend without requiring ENCODE_ACCEL.
for backend in nvenc vaapi qsv; do
    ENCODE_VIDEO_CODEC="h264_$backend" encode_job "$job"
    get_args
    assert_option -c:v "h264_$backend"
    assert_no_option -crf
done
ENCODE_ACCEL=nvenc ENCODE_PRESET=p6 ENCODE_QUALITY=19 encode_job "$job"
get_args
assert_option -preset p6
assert_option -cq:v 19
ENCODE_ACCEL=vaapi ENCODE_DEVICE=/dev/dri/renderD129 \
    ENCODE_EXTRA_ARGS='-vf "scale=1280:-2,format=nv12,hwupload"' encode_job "$job"
get_args
assert_option -vaapi_device /dev/dri/renderD129
assert_option -vf 'scale=1280:-2,format=nv12,hwupload'
count=0
for arg in "${args[@]}"; do if [[ "$arg" == -vf ]]; then count=$((count + 1)); fi; done
[[ "$count" -eq 1 ]]
ENCODE_VIDEO_CODEC=mpeg4 encode_job "$job"
get_args
assert_no_option -crf
assert_no_option -preset
assert_no_option -tag:v
ENCODE_VIDEO_CODEC=libx265 encode_job "$job"
get_args
assert_option -tag:v hvc1
assert_option -crf 23
assert_option -preset medium
ENCODE_VIDEO_CODEC=hevc_nvenc encode_job "$job"
get_args
assert_option -tag:v hvc1
assert_option -c:v hevc_nvenc
ENCODE_VIDEO_CODEC=libsvtav1 encode_job "$job"
get_args
assert_option -crf 23
assert_option -preset medium
rm "$TEST_ROOT/args"
if ENCODE_ACCEL=vaapi ENCODE_PRESET=medium encode_job "$job"; then exit 1; fi
if ENCODE_ACCEL=cpu ENCODE_VIDEO_CODEC=h264_nvenc encode_job "$job"; then exit 1; fi
[[ ! -f "$TEST_ROOT/args" ]]
# An unavailable encoder never falls back or publishes an unverified file.
rm "$TEST_ROOT/verified" "$TEST_ROOT/published" "$TEST_ROOT/calls"
if ENCODE_ACCEL=nvenc FAIL_ENCODER=true encode_job "$job"; then exit 1; fi
[[ $(wc -l < "$TEST_ROOT/calls") -eq 1 ]]
[[ $(cat "$job/capture.bin") == 'original capture' ]]
[[ ! -f "$TEST_ROOT/verified" && ! -f "$TEST_ROOT/published" ]]
grep -q 'GPU unavailable' "$job/diagnostics/encode-0001.stderr"
printf 'PASS: CPU/NVENC/VAAPI/QSV options, device setup, codec inference and safe hardware failure\n'
