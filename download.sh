#!/bin/bash
set -euo pipefail

DOWNLOAD_DIR="/etc/streamlink/scratch/$MODE/$CHANNEL/download"
ENCODE_DIR="/etc/streamlink/scratch/$MODE/$CHANNEL/encode"
STORAGE_DIR="/storage"
CHECK_INTERVAL=30   #seconds between live checks
ACCESS_TOKEN=""
TOKEN_EXPIRES_AT=0  # epoch timestamp

# ------------------------------------------------------------
# Function: get a new twitch token
# ------------------------------------------------------------
get_new_twitch_token() {
    echo "[Twitch] Getting new app access token..." >&2
    local now
    now=$(date +%s)

    local resp
    resp=$(curl -s -X POST "https://id.twitch.tv/oauth2/token" \
        -d "client_id=${CLIENT_ID}" \
        -d "client_secret=${CLIENT_SECRET}" \
        -d "grant_type=client_credentials")

    ACCESS_TOKEN=$(echo "$resp" | jq -r '.access_token')
    local expires_in
    expires_in=$(echo "$resp" | jq -r '.expires_in')

    TOKEN_EXPIRES_AT=$(( now + expires_in - 60 ))  # Refresh 1 min early

    if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "null" ]]; then
        echo "[Twitch] ERROR: Could not obtain token." >&2
        exit 1
    fi

    echo "[Twitch] Token acquired, good until epoch ${TOKEN_EXPIRES_AT}" >&2
}

# ------------------------------------------------------------
# Function: ensure twitch token is valid
# ------------------------------------------------------------
ensure_twitch_token() {
    local now
    now=$(date +%s)

    if (( now >= TOKEN_EXPIRES_AT )); then
        get_new_twitch_token
    fi
}


# ------------------------------------------------------------
# Function: fetch twitch livestream info (returns JSON or empty)
# ------------------------------------------------------------
get_twitch_stream_info() {
    local tmp_body
    tmp_body=$(mktemp)

    # Call Twitch Helix API, capture HTTP code separately from body
    local http_code
    http_code=$(curl -s -o "$tmp_body" -w "%{http_code}" \
        -H "Client-ID: ${CLIENT_ID}" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        "https://api.twitch.tv/helix/streams?user_login=${CHANNEL}")

    if [[ "$http_code" != "200" ]]; then
        echo "[Twitch] Unexpected HTTP code from Helix: ${http_code}" >&2
        echo "{}"   # valid JSON so jq won't explode
        rm -f "$tmp_body"
        return
    fi

    # Only JSON body to stdout
    cat "$tmp_body"
    rm -f "$tmp_body"
}

# ------------------------------------------------------------
# Function: get a new kick token
# ------------------------------------------------------------
get_new_kick_token() {
    echo "[Twitch] Getting new app access token..." >&2
    local now
    now=$(date +%s)

    local resp
    resp=$(curl -s -X POST "https://id.kick.com/oauth/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "client_id=${CLIENT_ID}" \
        -d "client_secret=${CLIENT_SECRET}" \
        -d "grant_type=client_credentials")

    ACCESS_TOKEN=$(echo "$resp" | jq -r '.access_token')
    local expires_in
    expires_in=$(echo "$resp" | jq -r '.expires_in')

    TOKEN_EXPIRES_AT=$(( now + expires_in - 60 ))  # Refresh 1 min early

    if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "null" ]]; then
        echo "[Twitch] ERROR: Could not obtain token." >&2
        exit 1
    fi

    echo "[Twitch] Token acquired, good until epoch ${TOKEN_EXPIRES_AT}" >&2
}

# ------------------------------------------------------------
# Function: ensure kick token is valid
# ------------------------------------------------------------
ensure_kick_token() {
    local now
    now=$(date +%s)

    if (( now >= TOKEN_EXPIRES_AT )); then
        get_new_kick_token
    fi
}

# ------------------------------------------------------------
# Function: fetch kick channel info (returns JSON or empty)
# ------------------------------------------------------------
get_kick_channel_info() {
    local tmp_body
    tmp_body=$(mktemp)

    # Call Kick API, capture HTTP code separately from body
    local http_code
    http_code=$(curl -s -o "$tmp_body" -w "%{http_code}" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        "https://api.kick.com/public/v1/channels?slug=${CHANNEL}")

    if [[ "$http_code" != "200" ]]; then
        echo "[Kick] Unexpected HTTP code from Kick: ${http_code}" >&2
        echo "{}"   # valid JSON so jq won't explode
        rm -f "$tmp_body"
        return
    fi

    # Only JSON body to stdout
    cat "$tmp_body"
    rm -f "$tmp_body"
}

# ------------------------------------------------------------
# Function: notify Discord
# ------------------------------------------------------------
notify_discord() {
    local message="$1"

    curl -s -X POST \
        -H "Content-Type: application/json" \
        -d "{\"content\": \"${message}\"}" \
        "$DISCORD_WEBHOOK_URL" > /dev/null
}

# ------------------------------------------------------------
# Main loop
# ------------------------------------------------------------
echo "[Monitor] Starting live status loop for channel: $CHANNEL"

if [ "${YOUTUBE_SPLITTING:-false}" == "true" ]; then
    HLS_DURATION="--hls-duration 11h59m30s"
else
    HLS_DURATION=""
fi

if [ $MODE == "twitch" ]; then
    while true; do
        # Make sure token is valid in the PARENT shell
        ensure_twitch_token

        json=$(get_twitch_stream_info 2>/dev/null)
        live_count=$(echo "$json" | jq '.data | length')

        if (( live_count == 0 )); then
            echo "[Monitor] Channel ${CHANNEL} not live. Checking again in ${CHECK_INTERVAL}s."
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Extract info
        title=$(echo "$json" | jq -r '.data[0].title')
        stream_id=$(echo "$json" | jq -r '.data[0].id')
        author=$(echo "$json" | jq -r '.data[0].user_name')

        folder_date=$(date +%Y%m)
        episode_date=$(date +%d%H)

        # Optional: minimal sanitization to strip slashes only
        safe_title=${title//\//-}
        FILENAME="${author} - s${folder_date}e${episode_date} - ${safe_title} - {edition-${MODE}} - ${stream_id} .ts"
        outfile="$DOWNLOAD_DIR/$FILENAME"

        echo "[Monitor] $CHANNEL is LIVE on Twitch!"
        echo "[Monitor] Title: $title"
        echo "[Monitor] Output: $outfile"
        if [ "${DISCORD_WEBHOOK_URL:-}" != "" ]; then
            notify_discord "**${CHANNEL} is LIVE!**\nTitle: ${title}"
        fi

        /usr/local/bin/streamlink \
            --retry-streams 30 \
            -l debug \
            --output "$outfile" \
            --twitch-force-client-integrity \
            --twitch-api-header "Authorization=OAuth $TWITCH_USER_TOKEN"\
            --webbrowser true \
            --webbrowser-headless true \
            $HLS_DURATION \
            "twitch.tv/${CHANNEL}" best

        echo "[Monitor] Streamlink completed. Checking status again..."
        if [ "{$UPLOAD:-false}" == "false" ]; then
            if [ "{$ENCODE:-false}" == "true" ]; then
                mv "$outfile" "$ENCODE_DIR/$FILENAME"
                echo moved "$outfile" to "$ENCODE_DIR/$FILENAME"
            else
                mv "$outfile" "$ENCODE_DIR/$FILENAME"
                echo moved "$FILENAME" to "$ENCODE_DIR/$FILENAME"
                STREAMTITLE="$(basename "$FILENAME" | sed -E 's/^[^-]+ - s[0-9]+e[0-9]+ - //; s/ - [0-9]+\.ts$//; s/</＜/g; s/>/＞/g')"
                echo "ST is: "$STREAMTITLE""
                MP4=$(echo "$FILENAME" | sed 's/\.ts$/.mp4/')
                echo "MP4 is: "$MP4""
                MP4PATH=$(echo "$outfile" | sed 's/\.ts$/.mp4/')
                mv "$outfile" "$MP4PATH"
                echo "Will try and execute exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$MP4PATH""
                exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$MP4PATH"
                sleep 2
                FOLDERDATE=$(date +%Y%m)
                mkdir -p "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE"  
                mv "$MP4PATH" "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$MP4"
            fi
        fi
        sleep "$CHECK_INTERVAL"
    done
elif [ $MODE == "kick" ]; then
    while true; do
        ensure_kick_token
        json=$(get_kick_channel_info 2>/dev/null)
        live=$(echo "$json" | jq '.data[0].stream.is_live')
        if [ $live != "true" ]; then
            echo "[Monitor] Channel ${CHANNEL} not live. Checking again in ${CHECK_INTERVAL}s."
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Extract info
        title=$(echo "$json" | jq -r '.data[0].stream_title')
        stream_id=$(date -d "$(echo "$json" | jq -r '.data[0].stream.start_time')" +%s)
        author=$(echo "$json" | jq -r '.data[0].slug')

        folder_date=$(date +%Y%m)
        episode_date=$(date +%d%H)

        # Optional: minimal sanitization to strip slashes only
        safe_title=${title//\//-}
        FILENAME="${author} - s${folder_date}e${episode_date} - ${safe_title} - {edition-${MODE}} - ${stream_id} .ts"
        outfile="$DOWNLOAD_DIR/$FILENAME"

        echo "[Monitor] $CHANNEL is LIVE on Kick!"
        echo "[Monitor] Title: $title"
        echo "[Monitor] Output: $outfile"
        if [ "${DISCORD_WEBHOOK_URL:-}" != "" ]; then
            notify_discord "**${CHANNEL} is LIVE!**\nTitle: ${title}"
        fi

        /usr/local/bin/streamlink \
            --retry-streams 30 \
            -l debug \
            --output "$outfile" \
            --webbrowser true \
            --webbrowser-headless true \
            $HLS_DURATION \
            "kick.com/${CHANNEL}" best

        echo "[Monitor] Streamlink completed. Checking status again..."
        if [ "{$UPLOAD:-false}" == "false" ]; then
            if [ "{$ENCODE:-false}" == "true" ]; then
                mv "$outfile" "$ENCODE_DIR/$FILENAME"
                echo moved "$outfile" to "$ENCODE_DIR/$FILENAME"
            else
                mv "$outfile" "$ENCODE_DIR/$FILENAME"
                echo moved "$FILENAME" to "$ENCODE_DIR/$FILENAME"
                STREAMTITLE="$(basename "$FILENAME" | sed -E 's/^[^-]+ - s[0-9]+e[0-9]+ - //; s/ - [0-9]+\.ts$//; s/</＜/g; s/>/＞/g')"
                echo "ST is: "$STREAMTITLE""
                MP4=$(echo "$FILENAME" | sed 's/\.ts$/.mp4/')
                echo "MP4 is: "$MP4""
                MP4PATH=$(echo "$outfile" | sed 's/\.ts$/.mp4/')
                mv "$outfile" "$MP4PATH"
                echo "Will try and execute exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$MP4PATH""
                exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$MP4PATH"
                sleep 2
                FOLDERDATE=$(date +%Y%m)
                mkdir -p "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE"  
                mv "$MP4PATH" "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$MP4"
            fi
        fi
        sleep "$CHECK_INTERVAL"
    done
else
    echo "Unsupported mode"
fi