import json
import subprocess
from pathlib import Path

from .models import SubtitleStream

IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle"}


def probe_subtitles(
    video: Path,
    language: list[str] | None = None,
    forced_only: bool = False,
) -> list[SubtitleStream]:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-select_streams", "s",
        str(video),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    data = json.loads(result.stdout)
    streams = []

    for s in data.get("streams", []):
        codec = s.get("codec_name", "")
        if codec not in IMAGE_SUBTITLE_CODECS:
            continue

        lang = s.get("tags", {}).get("language")
        forced = s.get("disposition", {}).get("forced", 0) == 1
        title = s.get("tags", {}).get("title")

        stream = SubtitleStream(
            index=s["index"],
            codec=codec,
            language=lang,
            forced=forced,
            source_file=video,
            title=title,
        )

        if language and lang not in language:
            continue
        if forced_only and not forced:
            continue

        streams.append(stream)

    return streams
