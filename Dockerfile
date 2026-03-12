FROM python:3.11-slim

# Install ffmpeg and CIFS/SMB utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    cifs-utils \
    && rm -rf /var/lib/apt/lists/*

# Create mount point for SMB shares
RUN mkdir -p /mnt/smb

WORKDIR /app

# Install Python dependencies first (better cache layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir pyyaml httpx Pillow rich

# Copy application code and install
COPY . .
RUN pip install --no-cache-dir -e .

# Config directory
RUN mkdir -p /root/.config/subtitler

ENV SUBTITLER_DOCKER=1

EXPOSE 8642

CMD ["subtitler-gui"]
