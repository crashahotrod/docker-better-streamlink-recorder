#!/bin/bash
MONITORDIR="/etc/streamlink/scratch/$MODE/$CHANNEL/download"
ENCODE_DIR="/etc/streamlink/scratch/$MODE/$CHANNEL/encode"
STORAGE_DIR="/storage"
set +H
inotifywait -m -r --format '%w%f' --include ".*\.(ts|mp4)$" -e close_write "$MONITORDIR" | while read NEWFILE
do
        if [[ $NEWFILE = *.fragmented.mp4* ]]; then
                echo 'Skipping Fragmented File'
        else
                echo "$NEWFILE created"
                FILENAME="$(basename "$NEWFILE")"
                STREAMTITLE="$(echo "$FILENAME" | sed -E 's/^[^-]+ - s[0-9]+e[0-9]+ - | - \{[^}]*\}| - [0-9]+|\.(ts|mp4)$//g; s/</＜/g; s/>/＞/g')"
                echo "Stream title is: $STREAMTITLE"
                UPLOADDATE="$(date +%m/%d/%Y)"
                UPLOADTITLE="$UPLOADDATE - $STREAMTITLE"
                echo "Upload Title is: $UPLOADTITLE"
                /etc/youtubeuploader -title "$UPLOADTITLE" -privacy "public" -filename "$NEWFILE" -description "Uploaded Automatically by $UPLOAD_BOT_NAME"
                sleep 2
                echo "Finished YT Upload"
                if [ "${ENCODE:-false}" == "true" ]; then
                        mv "$outfile" "$ENCODE_DIR/$FILENAME"
                        echo moved "$outfile" to "$ENCODE_DIR/$FILENAME"
                else
                        FOLDERDATE=$(date +%Y%m)
                        mkdir -p "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE"
                        mv "$NEWFILE" "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$FILENAME"
                        echo moved "$NEWFILE" to "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$FILENAME"
                        echo "Will try and execute exiftool -title=\"$STREAMTITLE\" -api largefilesupport=1 -overwrite_original \"$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$FILENAME\""
                        exiftool -title="$STREAMTITLE" -api largefilesupport=1 -overwrite_original "$STORAGE_DIR/$CHANNEL/Season $FOLDERDATE/$FILENAME"
                fi
        fi
done
