FROM python:3.12-slim

# System deps: ffmpeg, Node.js (for yt-dlp YouTube JS challenges), libs for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
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
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Cloudflare WARP proxy (wireproxy + wgcf for credential generation)
# Best-effort: if downloads fail, app still works without WARP.
RUN curl -fsSL "https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz" \
    | tar xz -C /usr/local/bin wireproxy || echo "wireproxy download failed"
RUN curl -fsSL "https://github.com/ViRb3/wgcf/releases/download/v2.2.32/wgcf_2.2.32_linux_amd64" \
    -o /usr/local/bin/wgcf && chmod +x /usr/local/bin/wgcf || echo "wgcf download failed"

# Store Playwright browsers alongside app code (smaller image)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# Copy app code
COPY . .
RUN chmod +x start-warp.sh

EXPOSE 8000

CMD ["./start-warp.sh"]
