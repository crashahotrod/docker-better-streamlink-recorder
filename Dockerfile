FROM python:3.12.2
LABEL org.opencontainers.image.authors="crashahotrod@gmail.com"
ENV streamlinkCommit=5a83a3806b5941639c3751ac15a9fed175019b31
RUN apt-get update && apt-get install supervisor python3-pip jq inotify-tools ffmpeg exiftool -y
RUN pip3 install --upgrade git+https://github.com/streamlink/streamlink.git@${streamlinkCommit}
RUN mkdir -p /storage
RUN mkdir -p /etc/streamlink/tools
RUN mkdir -p /etc/streamlink/scratch

COPY ./download.sh /etc/streamlink/tools/
COPY ./encode.sh /etc/streamlink/tools/
COPY ./supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]