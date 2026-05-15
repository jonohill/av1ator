FROM mwader/static-ffmpeg:8.1.1 AS ffmpeg

FROM python:3.13-alpine

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app
COPY av1ator/ /app/av1ator/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

VOLUME ["/input", "/output"]

ENTRYPOINT ["python3", "-m", "av1ator.batch", "/input", "/output"]
