FROM python:3.12.2
LABEL org.opencontainers.image.authors="crashahotrod@gmail.com"
ARG YTU_RELEASE=v1.25.5
ARG YTU_SHORT="${YTU_RELEASE#v}"
ARG BINARY_DOWNLOAD_URL="https://github.com/porjo/youtubeuploader/releases/download/${YTU_RELEASE}/youtubeuploader_${YTU_SHORT}_Linux_amd64.tar.gz"
RUN curl -L -o youtubeuploader.tar.gz "${BINARY_DOWNLOAD_URL}"
RUN tar -xzf youtubeuploader.tar.gz -C /etc youtubeuploader
ENV streamlinkCommit=5a83a3806b5941639c3751ac15a9fed175019b31
RUN apt-get update && apt-get install supervisor python3-pip jq inotify-tools ffmpeg exiftool -y
RUN pip3 install --upgrade git+https://github.com/streamlink/streamlink.git@${streamlinkCommit}
RUN mkdir -p /config
RUN mkdir -p /storage
RUN mkdir -p /etc/streamlink/tools
RUN mkdir -p /etc/streamlink/scratch

COPY ./download.sh /etc/streamlink/tools/
COPY ./encode.sh /etc/streamlink/tools/
COPY ./upload.sh /etc/streamlink/tools/
COPY ./supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /etc/streamlink/tools/download.sh
RUN chmod +x /etc/streamlink/tools/encode.sh
RUN chmod +x /etc/streamlink/tools/upload.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]