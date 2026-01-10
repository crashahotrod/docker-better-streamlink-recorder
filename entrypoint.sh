#!/bin/bash
set -e

ErrorPresent=0

check_required_var() {
    local var_name="$1"
    if [ -z "${!var_name}" ]; then
        echo "ERROR: Required environment variable $var_name is not set." >&2
        ErrorPresent=1
    fi
}
check_required_var MODE
check_required_var CHANNEL
check_required_var CLIENT_ID
check_required_var CLIENT_SECRET
check_required_var USER_NAME
if [ $MODE == "twitch" ]; then
    check_required_var TWITCH_USER_TOKEN
fi

mkdir -p /config /storage /etc/streamlink/scratch
chown -R "$USER_NAME:$USER_NAME" /config /storage /etc/streamlink/scratch

check_required_dir() {
    local dir_name="$1"
    if gosu $USER_NAME touch "${dir_name}/.write_test" 2>/dev/null; then
        rm -f "${dir_name}/.write_test"
    else
        echo "ERROR: The mounted directory ${dir_name} is NOT writeable by $USER_NAME." >&2
        ErrorPresent=1
    fi
}
check_required_dir /etc/streamlink/scratch
check_required_dir /storage
check_required_dir /config

check_required_file() {
    local file_name="$1"
    if gosu "$USER_NAME" test -f "$file_name" && gosu "$USER_NAME" test -r "$file_name"; then
        return 0
    else
        echo "ERROR: Required file ${file_name} does not exist or is not readable by $USER_NAME." >&2
        ErrorPresent=1
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

if [ "$ErrorPresent" -eq 1 ]; then
    sleep 30
    exit 1
fi
chown -R "$USER_NAME:$USER_NAME" /etc/supervisor/conf.d/
chmod 444 /etc/supervisor/conf.d/supervisord.conf
chmod 666 /dev/stdout /dev/stderr
chown "$USER_NAME:$USER_NAME" /var/run/dbus
mkdir -p /etc/streamlink/scratch/$MODE/$CHANNEL/{encode,download}
mkdir -p /var/run/dbus
gosu $USER_NAME dbus-launch --sh-syntax > /tmp/dbus_env
if [ -f /tmp/dbus_env ]; then
    . /tmp/dbus_env
    export DBUS_SESSION_BUS_ADDRESS
    export DBUS_SESSION_BUS_PID
    echo "DBus started for $USER_NAME user at: $DBUS_SESSION_BUS_ADDRESS"
fi
echo "Starting application as $USER_NAME (UID: $(id -u $USER_NAME))..."
exec gosu $USER_NAME "$@"