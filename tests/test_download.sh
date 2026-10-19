#!/bin/bash
# Exercise real request/parsing code with deterministic curl responses.
# Imported production functions are replaced only after the HTTP checks.
# shellcheck disable=SC2218
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../download.sh
source "$ROOT/download.sh"
TEST_ROOT=$(mktemp -d)
trap 'find "$TEST_ROOT" -depth -delete' EXIT
export MODE=twitch CHANNEL=monitor CLIENT_ID='client id' CLIENT_SECRET='client secret'
export TMPDIR="$TEST_ROOT/http"
mkdir -- "$TMPDIR"
TOKEN_RESPONSE='{"access_token":"first-token","expires_in":3600}'
API_RESPONSE='{"data":[]}'
HTTP_STATUS=200
TRANSPORT_FAILURE=false
TEST_NOW=10000
date() {
    if [[ "${1:-}" == +%s ]]; then printf '%s\n' "$TEST_NOW"; else command date "$@"; fi
}
curl() {
    local body='' previous='' arg count=0
    [[ ! -f "$TEST_ROOT/count" ]] || read -r count < "$TEST_ROOT/count"
    printf '%s\n' "$((count + 1))" > "$TEST_ROOT/count"
    printf '%s\0' "$@" > "$TEST_ROOT/request"
    for arg in "$@"; do
        if [[ "$previous" == -o ]]; then body="$arg"; fi
        previous="$arg"
    done
    [[ "$TRANSPORT_FAILURE" != true ]] || return 7
    if [[ -n "$body" ]]; then
        printf '%s' "$API_RESPONSE" > "$body"
        printf '%s' "$HTTP_STATUS"
    else
        printf '%s' "$TOKEN_RESPONSE"
    fi
}
refresh_token
[[ "$ACCESS_TOKEN" == first-token && "$TOKEN_EXPIRES_AT" -eq 13540 ]]
mapfile -d '' -t request < "$TEST_ROOT/request"
[[ " ${request[*]} " == *' https://id.twitch.tv/oauth2/token '* ]]
[[ " ${request[*]} " == *' client_id=client id '* && " ${request[*]} " == *' client_secret=client secret '* ]]
TOKEN_RESPONSE='{"access_token":"second-token","expires_in":120}'
refresh_token
[[ $(cat "$TEST_ROOT/count") -eq 1 && "$ACCESS_TOKEN" == first-token ]]
TOKEN_EXPIRES_AT=0
MODE=kick
refresh_token
[[ "$ACCESS_TOKEN" == second-token && $(cat "$TEST_ROOT/count") -eq 2 ]]
mapfile -d '' -t request < "$TEST_ROOT/request"
[[ " ${request[*]} " == *' https://id.kick.com/oauth/token '* ]]
TEST_NOW=$((TOKEN_EXPIRES_AT + 1))
refresh_token
[[ $(cat "$TEST_ROOT/count") -eq 3 ]]
for TOKEN_RESPONSE in '{}' '{"access_token":"","expires_in":10}' \
    '{"access_token":"token","expires_in":0}' '{"access_token":"token","expires_in":-1}' \
    '{"access_token":"token","expires_in":"3600"}' '{"access_token":"token","expires_in":1.5}' 'invalid'; do
    TOKEN_EXPIRES_AT=0
    if refresh_token 2>/dev/null; then echo 'Invalid token response accepted' >&2; exit 1; fi
done
TOKEN_RESPONSE='{"access_token":"short-token","expires_in":1}'
refresh_token
[[ "$TOKEN_EXPIRES_AT" -eq $((TEST_NOW + 1)) ]]
TOKEN_EXPIRES_AT=0 TRANSPORT_FAILURE=true
if refresh_token; then echo 'Transport failure accepted' >&2; exit 1; fi
TRANSPORT_FAILURE=false ACCESS_TOKEN=api-token
for MODE in twitch kick; do
    result=$(fetch_stream_info)
    [[ "$result" == "$API_RESPONSE" ]]
    mapfile -d '' -t request < "$TEST_ROOT/request"
    [[ " ${request[*]} " == *' Authorization: Bearer api-token '* ]]
    if [[ "$MODE" == twitch ]]; then
        [[ " ${request[*]} " == *' Client-ID: client id '* && "${request[-1]}" == *'user_login=monitor' ]]
    else
        [[ " ${request[*]} " != *'Client-ID:'* && "${request[-1]}" == *'slug=monitor' ]]
    fi
    for HTTP_STATUS in 401 403 429 500; do
        if fetch_stream_info > /dev/null; then echo 'HTTP failure accepted' >&2; exit 1; else status=$?; fi
        expected=1
        if [[ "$HTTP_STATUS" == 401 ]]; then expected=2; fi
        [[ "$status" -eq "$expected" ]]
    done
    HTTP_STATUS=200 TRANSPORT_FAILURE=true
    if fetch_stream_info; then echo 'API transport failure accepted' >&2; exit 1; fi
    TRANSPORT_FAILURE=false
done
[[ -z $(find "$TMPDIR" -mindepth 1 -print -quit) ]]

# Monitor decisions must not create recordings for failures or offline responses.
refresh_token() { [[ "${FAIL_TOKEN:-false}" != true ]]; }
fetch_stream_info() { printf '%s' "$API_RESPONSE"; return "${API_EXIT:-0}"; }
record_stream() { printf '%s\0' "$@" >> "$TEST_ROOT/recordings"; }
for MODE in twitch kick; do
    FAIL_TOKEN=true
    if monitor_once; then echo 'Failed token accepted by monitor' >&2; exit 1; fi
    FAIL_TOKEN=false API_EXIT=2 TOKEN_EXPIRES_AT=9999999999
    if monitor_once; then exit 1; fi
    [[ "$TOKEN_EXPIRES_AT" -eq 0 ]]
    API_EXIT=1 TOKEN_EXPIRES_AT=9999999999
    if monitor_once; then exit 1; fi
    [[ "$TOKEN_EXPIRES_AT" -eq 9999999999 ]]
    API_EXIT=0 API_RESPONSE='{"data":[]}'
    monitor_once
    if [[ "$MODE" == kick ]]; then
        API_RESPONSE='{"data":[{"stream":{"is_live":false}}]}'
        monitor_once
    fi
    API_RESPONSE='invalid'
    if monitor_once 2>/dev/null; then echo 'Invalid JSON accepted' >&2; exit 1; fi
done
[[ ! -e "$TEST_ROOT/recordings" ]]
MODE=twitch API_RESPONSE='{"data":[{"title":"quote \" and\nnewline","user_name":"Author","id":"123"}]}'
monitor_once
mapfile -d '' -t recorded < "$TEST_ROOT/recordings"
[[ "${recorded[0]}" == $'quote " and\nnewline' && "${recorded[1]}" == Author && "${recorded[2]}" == 123 ]]
MODE=kick API_RESPONSE='{"data":[{"stream_title":"Kick title","slug":"author","stream":{"is_live":true,"start_time":"2026-10-18T00:00:00Z"}}]}'
monitor_once
mapfile -d '' -t recorded < "$TEST_ROOT/recordings"
[[ "${recorded[3]}" == 'Kick title' && "${recorded[4]}" == author && "${recorded[5]}" == "$(date -d 2026-10-18T00:00:00Z +%s)" ]]
before=$(wc -c < "$TEST_ROOT/recordings")
for API_RESPONSE in '{"data":[{"stream_title":"title","slug":"author","stream":{"is_live":true}}]}' \
    '{"data":[{"stream_title":"title","slug":"author","stream":{"is_live":true,"start_time":"bad-date"}}]}'; do
    if monitor_once 2>/dev/null; then echo 'Missing stream identity accepted' >&2; exit 1; fi
done
[[ $(wc -c < "$TEST_ROOT/recordings") -eq "$before" ]]

for MODE in twitch kick; do
    for YOUTUBE_SPLITTING in true false; do
        for TWITCH_USER_TOKEN in '' 'token with spaces'; do
            build_streamlink_args
            auth=0 duration=0
            for arg in "${STREAMLINK_ARGS[@]}"; do
                if [[ "$arg" == 'Authorization=OAuth token with spaces' ]]; then auth=$((auth + 1)); fi
                if [[ "$arg" == 11h59m30s ]]; then duration=$((duration + 1)); fi
            done
            if [[ "$MODE" == twitch && -n "$TWITCH_USER_TOKEN" ]]; then [[ "$auth" -eq 1 ]]; else [[ "$auth" -eq 0 ]]; fi
            if [[ "$YOUTUBE_SPLITTING" == true ]]; then [[ "$duration" -eq 1 ]]; else [[ "$duration" -eq 0 ]]; fi
        done
    done
done
STREAMLINK_EXTRA_ARGS='--hls-live-restart --stream-segment-threads 2'
build_streamlink_args
has_restart=0 threads=0
for arg in "${STREAMLINK_ARGS[@]}"; do
    if [[ "$arg" == '--hls-live-restart' ]]; then has_restart=$((has_restart + 1)); fi
    if [[ "$arg" == '--stream-segment-threads' ]]; then threads=$((threads + 1)); fi
done
[[ "$has_restart" -eq 1 && "$threads" -eq 1 ]]
unset STREAMLINK_EXTRA_ARGS
printf 'PASS: token requests/cache, HTTP errors, offline/live decisions, splitting, authentication, and extra args\n'
