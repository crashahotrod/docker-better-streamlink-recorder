#!/bin/bash
MONITORDIR="/etc/streamlink/scratch/$CHANNEL/encode"
STORAGE_DIR="/storage"

#: "${CHANNEL:?Need CHANNEL}"

inotifywait -m -r --format '%w%f' --include ".*ts" -e MOVED_TO "$MONITORDIR" | while read NEWFILE
do
    echo "INW is "$NEWFILE""
    STREAMTITLE="$(basename "$NEWFILE" | sed -E 's/^[^-]+ - s[0-9]+e[0-9]+ - //; s/ - [0-9]+\.ts$//; s/</＜/g; s/>/＞/g')"
    echo "ST is: "$STREAMTITLE""
    FILENAME="$(basename "$NEWFILE")"
    echo "FN is: $FILENAME"
    MP4=$(echo "$FILENAME" | sed 's/\.ts$/.mp4/')
    echo "MP4 is: "$MP4""
    MP4PATH=$(echo "$NEWFILE" | sed 's/\.ts$/.mp4/')
    echo "MP4PATH is "$MP4PATH""
    echo "Will try and execute ffmpeg -i "$NEWFILE" -c:v libx264 -crf 23 -preset medium -c:a aac -b:a 128k "$MP4PATH""
    ffmpeg -i "$NEWFILE" -c:v libx264 -crf 23 -preset medium -c:a aac -b:a 128k "$MP4PATH"
    echo "Will try and execute rm -rf "$NEWFILE""
    rm -rf "$NEWFILE"
    echo "Will try and execute exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$MONITORDIR/$MP4""
    exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$MONITORDIR/$MP4"
    sleep 2
    FOLDERDATE=$(date +%Y%m)
    mkdir -p "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE"  
    mv "$MP4PATH" "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$MP4"
done
