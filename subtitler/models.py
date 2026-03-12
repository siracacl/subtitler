from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SubtitleStream:
    index: int
    codec: str  # "hdmv_pgs_subtitle" or "dvd_subtitle"
    language: str | None
    forced: bool
    source_file: Path
    title: str | None = None

    @property
    def is_pgs(self) -> bool:
        return self.codec == "hdmv_pgs_subtitle"

    @property
    def is_vobsub(self) -> bool:
        return self.codec == "dvd_subtitle"

    @property
    def lang_code(self) -> str:
        return self.language or "und"


@dataclass
class SubtitleFrame:
    index: int
    start_ms: int
    end_ms: int
    image_bytes: bytes  # raw PNG


@dataclass
class OCRResult:
    frame: SubtitleFrame
    text: str
