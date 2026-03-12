import json
import subprocess
from pathlib import Path

from .models import SubtitleStream


def extract_stream(stream: SubtitleStream, work_dir: Path) -> Path:
    if stream.is_pgs:
        return _extract_pgs(stream, work_dir)
    elif stream.is_vobsub:
        return _extract_vobsub_mkv(stream, work_dir)
    else:
        raise ValueError(f"Unsupported codec: {stream.codec}")


def _extract_pgs(stream: SubtitleStream, work_dir: Path) -> Path:
    out = work_dir / f"stream_{stream.index}.sup"
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(stream.source_file),
        "-map", f"0:{stream.index}",
        "-c:s", "copy",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _extract_vobsub_mkv(stream: SubtitleStream, work_dir: Path) -> Path:
    """Extract VobSub into a single-track MKV for subtitle rendering."""
    out = work_dir / f"stream_{stream.index}.mkv"
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(stream.source_file),
        "-map", f"0:{stream.index}",
        "-c:s", "dvd_subtitle",
        "-f", "matroska",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out
