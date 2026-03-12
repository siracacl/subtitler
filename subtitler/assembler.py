from pathlib import Path

from .models import OCRResult, SubtitleStream


def _format_ts_vtt(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _format_ts_srt(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_output_path(
    stream: SubtitleStream,
    output_format: str,
    output_dir: str | None = None,
) -> Path:
    video = stream.source_file
    stem = video.stem
    lang = stream.lang_code

    suffix = f".{lang}"
    if stream.forced:
        suffix += ".forced"
    suffix += f".{output_format}"

    if output_dir:
        return Path(output_dir) / (stem + suffix)
    return video.parent / (stem + suffix)


def write_subtitles(
    results: list[OCRResult],
    output_path: Path,
    output_format: str,
):
    # Filter out empty results and errors
    valid = [r for r in results if r.text and not r.text.startswith("[OCR ERROR")]

    if not valid:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        if output_format == "vtt":
            f.write("WEBVTT\n\n")
            for r in valid:
                start = _format_ts_vtt(r.frame.start_ms)
                end = _format_ts_vtt(r.frame.end_ms)
                f.write(f"{start} --> {end}\n")
                f.write(f"{r.text}\n\n")
        else:  # srt
            for i, r in enumerate(valid, 1):
                start = _format_ts_srt(r.frame.start_ms)
                end = _format_ts_srt(r.frame.end_ms)
                f.write(f"{i}\n")
                f.write(f"{start} --> {end}\n")
                f.write(f"{r.text}\n\n")
