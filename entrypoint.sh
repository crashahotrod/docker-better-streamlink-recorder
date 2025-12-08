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
check_required_var MODE
check_required_var CHANNEL
check_required_var CLIENT_ID
check_required_var CLIENT_SECRET
if [ $MODE == "twitch" ]; then
    check_required_var TWITCH_USER_TOKEN
fi

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
check_required_dir /config

check_required_file() {
    local file_name="$1"
    if [ -f "$file_name" ]; then
        return 0
    else
        echo "ERROR: Required file ${file_name} does not exist." >&2
        ErrorPresent=0
        return 1
    fi
}

if [ "{$UPLOAD:-false}" == "true" ]; then
    check_required_var UPLOAD_BOT_NAME
    sed -i '/^\[program:streamlink_upload\]/,/^\[/ {/autostart=false/ s/autostart=false/autostart=true/}' /etc/supervisor/conf.d/supervisord.conf
    if check_required_file "/config/youtubeuploader_client_secrets.json" && check_required_file "/config/youtubeuploader_request.token"; then
        ln -s /config/youtubeuploader_client_secrets.json /etc/client_secrets.json
        ln -s /config/youtubeuploader_request.token /etc/request.token
    fi
fi

if [ "{$ENCODE:-false}" == "true" ]; then
    sed -i '/^\[program:streamlink_encode\]/,/^\[/ {/autostart=false/ s/autostart=false/autostart=true/}' /etc/supervisor/conf.d/supervisord.conf
fi

if [ "$ErrorPresent" -eq 0 ]; then
    exit 1
fi

mkdir -p /etc/streamlink/scratch/$MODE/$CHANNEL/{encode,download}
exec "$@"