FROM python:3.12-slim

# System deps: ffmpeg for audio/video, libs needed by Playwright's Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    libxshmfence1 \
    libwoff1 \
    libopus0 \
    libwebpdemux2 \
    libwebpmux3 \
    libpng-dev \
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

# Store Playwright browsers alongside app code (smaller image)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# Copy app code
COPY . .

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "300", "--keep-alive", "5", "app:app"]
