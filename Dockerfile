# Use official Python base image
FROM python:3.12-slim-bookworm

# System dependencies for Playwright + curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + the OS libs Playwright needs
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

RUN mkdir -p downloads

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Default port (Railway/Render override this via $PORT)
ENV PORT=8080
EXPOSE 8080

CMD ["/bin/bash", "/app/entrypoint.sh"]
