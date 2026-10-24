FROM python:3.12-slim

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG APP_VERSION=1.0.0

ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

LABEL org.opencontainers.image.title="Emby Poster Worker" \
      org.opencontainers.image.description="Render rating and quality badges onto Emby posters, then publish them as the item's Primary image." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy every module in one go: adding a new .py file must not require touching
# the Dockerfile (a missing COPY used to surface as an ImportError at runtime).
COPY *.py ./
COPY assets ./assets
COPY web ./web

CMD ["python", "app.py"]
