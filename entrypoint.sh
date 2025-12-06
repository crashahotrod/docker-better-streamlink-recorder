#!/bin/bash
set -e

ErrorPresent=1

check_required_var() {
    local var_name="$1"
    if [ -z "${!var_name}" ]; then
        echo "ERROR: Required environment variable $var_name is not set." >&2
        ErrorPresent=0
    fi
}
check_required_var CHANNEL
check_required_var TWITCH_CLIENT_ID
check_required_var TWITCH_CLIENT_SECRET
check_required_var TWITCH_USER_TOKEN

check_required_dir() {
    local dir_name="$1"
    if [ -w "$dir_name" ]; then
        rm -f "{$dir_name}/.write_test"
    else
        echo "ERROR: The mounted directory ${dir_name} is NOT writeable." >&2
        ErrorPresent=0
    fi
}
check_required_dir /etc/streamlink/scratch
check_required_dir /storage

if [ "$ErrorPresent" -eq 0 ]; then
    exit 1
fi

mkdir -p /etc/streamlink/scratch/$CHANNEL/{encode,download}
exec "$@"