# docker-better-streamlink-recorder
Automated Dockerfile to record Twitch & Kick livestreams with streamlink
https://hub.docker.com/r/crashahotrod/docker-better-streamlink-recorder

## Features
 - Support for both Twitch & Kick
 - Files are saved in a Plex friendly format for better playback and organization
 - Support for upload to YouTube
 - Support for Discord go live notifications

## Environment variables
### Required
'MODE' - Set to the streaming service you'd like to monitor Kick or Twitch

'CHANNEL' - Put the name you'd like to monitor and record here

'CLIENT_ID' - Set to the client id issued by the streaming service you'd like to monitor

'CLIENT_SECRET' - Set to the client secret issued by the streaming service you'd like to monitor


### Optional
'REMUX' - Flag to enable or disable live remux to mp4 (true or false)

'ENCODE' - Flag to enable or disable after record re-encode to H.264 medium preset set to (true or false)

'UPLOAD' - Flag to enable or disable upload to YouTube set to (true or false)

'UPLOAD_BOT_NAME' - Set to the name of the bot you'd like to add to the YouTube description

'YOUTUBE_SPLITTING' - Flag to enable 12hr splitting of recording for YouTube uploads (true or false)

'DISCORD_WEBHOOK_URL' - Set to the full URL of the Discord Webhook you'd like to use for go live notifications

'TWITCH_USER_TOKEN' -  Set to token obtained from linked instructions to disable ads on subscribed channels https://streamlink.github.io/cli/plugins/twitch.html#authentication


## Volumes
'/etc/streamlink/scratch' - Redirect this folder to the path you'd like streamlink to use as a scratch disk

'/storage' - Redirect this folder to the path you'd like streamlink to store the completed recordings

'/config' - Optional: Redirect this folder to the path containing youtubeuploader_client_secrets.json and youtubeuploader_request.token