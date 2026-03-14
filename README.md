# Subtitler

> **Disclaimer: Partly AI-coded**

OCR image-based subtitles (PGS/VobSub) from video files using vision LLMs. Extracts Blu-ray and DVD subtitle tracks, sends the images to a vision model, and outputs searchable VTT or SRT files.

Works with any OpenAI-compatible API: OpenRouter, LM Studio, Ollama, or your own endpoint.

## Why this exists

[Subtitle Edit](https://github.com/SubtitleEdit/subtitleedit) is an excellent tool for working with subtitles - including OCR of image-based formats. However, if you have a large media library with hundreds of video files across multiple seasons and languages, processing them one by one through a desktop GUI is not practical.

Subtitler was built to fill that gap: a batch-capable, headless-friendly tool that can chew through an entire library unattended. Point it at a folder (or a network share), configure your API servers, hit start, and come back hours later to a complete set of text subtitles. It leverages modern vision LLMs instead of traditional OCR engines like Tesseract, which tend to perform significantly better on the varied fonts, colors, and backgrounds found in subtitle images - especially across multiple languages.

The VobSub parser is a direct port of Subtitle Edit's own decoder, so credit where credit is due.

## Security and safety notes

- **No authentication**: The web GUI has no login or access control. Do not expose it to the public internet - run it only on trusted internal networks or behind a reverse proxy with authentication.
- **Privileged container**: The Docker container runs in `privileged` mode to support SMB share mounting. This gives the container full access to the host system. If you don't need network shares, remove `privileged: true` from your `docker-compose.yml`.
- **Backup your files**: While subtitler has been thoroughly tested on hundreds of files without issues, it works with ffmpeg to extract streams from your video files. It is designed to leave your original files just that, original (i.e. nothing is written or changed). However, the possibility of something going wrong - however unlikely - cannot be entirely ruled out. A solid backup strategy for your media library is always recommended before batch-processing large collections.

## Features

- **PGS & VobSub** parsing with zero external subtitle libraries (pure Python binary decoders)
- **Web GUI** with real-time progress, live ETA, file browser, and stop/resume
- **Multi-server OCR** - distribute work across multiple API endpoints simultaneously
- **Graceful failover** - if a server goes down, work is redistributed to remaining servers
- **SMB/CIFS mounting** - browse and process files from network shares directly
- **Time estimation** - benchmark OCR speed before committing to a full run
- **CLI mode** for scripting and automation
- **Language filtering** and forced-subtitle-only mode
- **Skips existing outputs** - safe to re-run without reprocessing

## Quick Start (Docker)

### Pre-built image (recommended)

```bash
# 1. Create a docker-compose.yml
cat > docker-compose.yml <<'EOF'
services:
  subtitler:
    image: ghcr.io/yourusername/subtitler:latest
    ports:
      - "8642:8642"
    privileged: true
    env_file: stack.env
    volumes:
      - /path/to/your/videos:/videos
EOF

# 2. Create your config
cat > stack.env <<'EOF'
PYTHONUNBUFFERED=1
# Optional - servers can be configured in the web UI instead
# SUBTITLER_API_KEY=sk-or-v1-your-key-here
EOF

# 3. Run
docker compose up -d
```

Open `http://localhost:8642` in your browser.

### Build from source

```bash
git clone https://github.com/yourusername/subtitler.git
cd subtitler
cp stack.env.example stack.env
docker compose up --build
```

The image is automatically built for `linux/amd64` and `linux/arm64` on every push to main, tagged as `latest` and `YYYYMMDDHHMM`.

## Setup

### Docker (recommended)

The Docker image includes ffmpeg and SMB/CIFS support out of the box.

```bash
docker compose up --build
```

The container runs in privileged mode to enable SMB share mounting. If you don't need network shares, you can remove `privileged: true` from `docker-compose.yml`.

To mount a local video folder into the container, add a volume:

```yaml
services:
  subtitler:
    build: .
    ports:
      - "8642:8642"
    privileged: true
    env_file: stack.env
    volumes:
      - /path/to/your/videos:/videos
```

### Local install

Requires Python 3.11+ and ffmpeg.

```bash
# Install ffmpeg (macOS)
brew install ffmpeg

# Install ffmpeg (Debian/Ubuntu)
sudo apt install ffmpeg

# Install subtitler
pip install -e .

# Run the web GUI
subtitler-gui

# Or use the CLI
subtitler /path/to/videos
```

## Configuration

Settings are loaded in order of priority (highest first):

1. **Web GUI / CLI arguments**
2. **Environment variables** (`SUBTITLER_*`)
3. **Config file** (`subtitler.yaml` or `~/.config/subtitler/config.yaml`)
4. **Defaults**

### Environment variables

All environment variables are optional. API servers, models, and concurrency can also be configured through the web GUI (stored in browser localStorage).

| Variable | Default | Description | Also in GUI? |
|---|---|---|---|
| `SUBTITLER_API_KEY` | *(none)* | API key (fallback for CLI mode) | Yes (per server) |
| `SUBTITLER_BASE_URL` | `https://openrouter.ai/api/v1` | API endpoint (fallback for CLI mode) | Yes (per server) |
| `SUBTITLER_MODEL` | `google/gemma-3-27b-it` | Model to use (fallback for CLI mode) | Yes (per server) |
| `SUBTITLER_CONCURRENCY` | `10` | Max parallel API requests (CLI mode) | Yes (per server) |
| `SUBTITLER_OUTPUT_FORMAT` | `vtt` | Output format: `vtt` or `srt` | Yes |
| `SUBTITLER_LANGUAGE` | *(all)* | Comma-separated language codes (e.g. `eng,fre`) | Yes |
| `SUBTITLER_FORCED_ONLY` | `false` | Only process forced subtitle tracks | Yes |
| `SUBTITLER_OUTPUT_DIR` | *(same as video)* | Custom output directory | No |

> When using the web GUI, server configuration (API URL, key, model, concurrency) is managed per-server in the **API Servers** panel and does not require environment variables.

### SMB auto-mount (Docker)

Set these in `stack.env` to mount a network share on container startup:

| Variable | Description |
|---|---|
| `SUBTITLER_SMB_SERVER` | Server hostname or IP |
| `SUBTITLER_SMB_SHARE` | Share name |
| `SUBTITLER_SMB_USER` | Username (optional, omit for guest) |
| `SUBTITLER_SMB_PASS` | Password |
| `SUBTITLER_SMB_DOMAIN` | Domain (optional) |

### Config file

```yaml
api_key: sk-or-v1-...
base_url: https://openrouter.ai/api/v1
model: google/gemma-3-27b-it
concurrency: 10
output_format: vtt
language: [eng, fre]
forced_only: false
```

## Web GUI

The GUI runs at `http://localhost:8642` and provides:

- **File browser** - navigate folders and select videos or directories
- **Stream scanner** - detects all image-based subtitle tracks with language codes
- **Multi-server configuration** - add multiple OCR endpoints with individual concurrency settings, saved in browser localStorage
- **Time estimation** - benchmarks 10 random subtitle frames to predict total processing time
- **Live progress** - real-time progress bars, current subtitle text preview, and running ETA with estimated finish time
- **Stop button** - gracefully cancels in-progress OCR work
- **Reconnect on reload** - refreshing the page reconnects to a running job without losing progress

### Multi-server setup

You can configure multiple API servers in the GUI to distribute OCR work. Each server has its own base URL, API key, model, and concurrency limit. The application distributes frames evenly across all enabled servers, which process them concurrently.

Example use case: run a local LM Studio instance (concurrency 3) alongside OpenRouter (concurrency 20) for a combined throughput of 23 concurrent requests.

If a server becomes unreachable during processing, its pending work is automatically redistributed to the remaining servers.

For local models (LM Studio, Ollama), leave the API Key field blank - no `Authorization` header will be sent.

## CLI

```bash
# Process all videos in a directory
subtitler /path/to/videos

# Specific file
subtitler /path/to/movie.mkv

# Filter by language
subtitler --language eng fre /path/to/videos

# Use SRT output format
subtitler --output-format srt /path/to/videos

# Custom API endpoint (e.g. local LM Studio)
subtitler --base-url http://localhost:1234/v1 --model qwen2.5-vl-7b /path/to/videos

# Preview without processing
subtitler --dry-run /path/to/videos

# Set concurrency
subtitler --concurrency 4 /path/to/videos
```

## Output

Output files are saved alongside the source video:

```
movie.mkv
movie.eng.vtt         # English subtitles
movie.fre.vtt         # French subtitles
movie.eng.forced.vtt  # Forced English subtitles
```

Existing output files are skipped on re-runs.

## Supported formats

### Input

| Format | Source | Description |
|---|---|---|
| PGS | Blu-ray | Presentation Graphic Stream (.sup), 90kHz timing, RLE-compressed RGBA |
| VobSub | DVD | MPEG-2 PS packets, 4-color SPU with transparency |

### Container formats

MKV, MP4, AVI, M4V, TS, M2TS, VOB, MPG, MPEG, WMV, OGM, MOV

### Output

| Format | Extension | Timing format |
|---|---|---|
| WebVTT | `.vtt` | `HH:MM:SS.mmm` |
| SubRip | `.srt` | `HH:MM:SS,mmm` |

## Recommended models

**Personal recommendation: `qwen/qwen2.5-vl-7b`** - Fast, accurate, and small enough to run locally on an M4 Mac Mini at ~22 tokens/s. In testing, it produced better OCR results than the larger Gemma 3 27B model, particularly with accented characters and multilingual text. It's the best balance of speed, accuracy, and resource usage for subtitle OCR. In general, I'd recommend to turn off thinking/reasoning (where applicable) - in my testing this lead to "corrected" subtitles, i.e. the LLM sometimes not returning text true to the original.

| Model | Best for | Notes |
|---|---|---|
| **Qwen 2.5 VL 7B** | **Local (LM Studio)** | **Best accuracy, runs on 16GB Macs** |
| Qwen 2.5 VL 32B | Local (LM Studio) | Needs 24GB+, slightly better on edge cases |
| Gemma 3 27B | Cloud (OpenRouter) | Good and cheap via API, less accurate than Qwen 2.5 VL locally |
| Qwen 3.5 9B | Local (LM Studio) | Fast but thinking mode should be disabled |

For local models on Apple Silicon, see the memory requirements:

| Model | Quantization | VRAM needed | ~tok/s (M4 Mini) |
|---|---|---|---|
| Qwen 2.5 VL 7B | Q4_K_M | ~5 GB | ~22 |
| Qwen 3.5 9B | Q4_K_M | ~6 GB | ~14 |
| Gemma 3 27B | Q4_K_M | ~18 GB | Won't fit (16GB) |
| Gemma 3 27B | Q6_K | ~23 GB | Won't fit (16GB) |

## Project structure

```
subtitler/
  cli.py          # Command-line interface
  web.py          # Web GUI with embedded HTML/JS
  config.py       # Configuration management
  ocr.py          # Vision LLM client (single & multi-server)
  probe.py        # ffprobe subtitle detection
  scanner.py      # Video file discovery
  extractor.py    # ffmpeg stream extraction
  assembler.py    # VTT/SRT file writer
  models.py       # Data classes
  progress.py     # CLI progress display
  parsers/
    pgs.py        # Blu-ray PGS binary parser
    vobsub.py     # DVD VobSub binary parser
```

## License

MIT
