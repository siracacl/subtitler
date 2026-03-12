from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts",
    ".vob", ".mpg", ".mpeg", ".wmv", ".ogm",
}


def scan_videos(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTENSIONS else []

    files = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            files.append(f)
    return files
