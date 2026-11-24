# syntax=docker/dockerfile:1
#
# Two stages: the UI is built with Node, then the API runs on Python 3.12 (the ML
# stack lags behind 3.14, so the image pins 3.12 even though the local venv is 3.14).
#
#   docker build -t transcribe .
#   docker run --rm -p 8000:8000 -v ${PWD}/data:/data transcribe
#
# Browser-agent support is opt-in because Playwright pulls ~400 MB of system deps:
#
#   docker build --build-arg INSTALL_BROWSER=1 -t transcribe:browser .

FROM node:22-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ARG INSTALL_BROWSER=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRANSCRIBE_DATA_DIR=/data \
    HF_HOME=/models \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1

WORKDIR /app

# libglib2.0-0 and libgomp1 are needed by opencv and ctranslate2 on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements-ml.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements-ml.txt

# Face model (232 KB) ships with the image; Whisper weights are downloaded on
# first use into /models, which is meant to be a volume.
ADD https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx /models/face_detection_yunet_2023mar.onnx

COPY backend /app/backend
COPY tools /app/tools
COPY --from=web /app/frontend/dist /app/frontend/dist

RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        pip install --no-cache-dir playwright && \
        python -m playwright install --with-deps chromium; \
    fi

VOLUME ["/data", "/models"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
