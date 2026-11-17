#!/bin/bash
# Verify shell-style quoting without expansion, including actual encoder argv.
# Literal expansion syntax is intentional test data.
# shellcheck disable=SC2016
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../encode.sh
source "$ROOT/encode.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
declare -a parsed_args=()
load_extra_args parsed_args ''
[[ ${#parsed_args[@]} -eq 0 ]]
load_extra_args parsed_args $' \t\n '
[[ ${#parsed_args[@]} -eq 0 ]]
load_extra_args parsed_args $'--first "two words" \'single quoted\' escaped\\ space "" \'café\' "line\nbreak" #literal \'$HOME\' \'*.ts\''
expected=(--first 'two words' 'single quoted' 'escaped space' '' 'café' $'line\nbreak' '#literal' '$HOME' '*.ts')
[[ ${#parsed_args[@]} -eq ${#expected[@]} ]]
for i in "${!expected[@]}"; do [[ "${parsed_args[i]}" == "${expected[i]}" ]]; done
load_extra_args parsed_args '"C:\recordings\file name.mp4"'
[[ ${#parsed_args[@]} -eq 1 && "${parsed_args[0]}" == 'C:\recordings\file name.mp4' ]]
for invalid in '--valid value "unterminated' "--valid value 'unterminated" "trailing\\"; do
    if parse_extra_args "$invalid" > "$TEST_ROOT/parse-output"; then echo 'Invalid quoting accepted' >&2; exit 1; fi
    [[ ! -s "$TEST_ROOT/parse-output" ]]
    if load_extra_args parsed_args "$invalid"; then echo 'Parser failure hidden by mapfile' >&2; exit 1; fi
    [[ ${#parsed_args[@]} -eq 0 ]]
done

job="$TEST_ROOT/recording.encoder"
mkdir -p "$job/parts" "$job/diagnostics"
cd -- "$TEST_ROOT"
ensure_normalized_job() { return 0; }
part_ids() { printf '0001\n'; }
part_file() { printf '%s/parts/0001.mp4\n' "$1"; }
part_committed() { return 1; }
verify_part() { return 0; }
publish_part() { return 0; }
remove_completed_job() { return 0; }
prune_old_recordings() { return 0; }
ffmpeg() { printf '%s\0' "$@" > "$TEST_ROOT/ffmpeg-args"; }
ENCODE_EXTRA_ARGS='-vf "scale=160:90,setsar=1" -metadata title="A café title" -metadata "comment=$(touch NEVER_RUN)"'
encode_job "$job"
mapfile -d '' -t encoder_args < "$TEST_ROOT/ffmpeg-args"
expected=(-vf 'scale=160:90,setsar=1' -metadata 'title=A café title' -metadata 'comment=$(touch NEVER_RUN)' -f mp4 "$job/parts/0001.encoded.mp4")
offset=$((${#encoder_args[@]} - ${#expected[@]}))
for i in "${!expected[@]}"; do [[ "${encoder_args[i+offset]}" == "${expected[i]}" ]]; done
[[ ! -f NEVER_RUN ]]
rm "$TEST_ROOT/ffmpeg-args"
ENCODE_EXTRA_ARGS='-vf "unterminated'
if encode_job "$job"; then echo 'Invalid encoder arguments accepted' >&2; exit 1; fi
[[ ! -f "$TEST_ROOT/ffmpeg-args" ]]
printf 'PASS: extra-argument quoting, literal tokens, empty arguments and encoder failure propagation\n'
