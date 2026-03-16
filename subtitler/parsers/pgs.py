"""
PGS/SUP subtitle parser — standalone, no external dependencies.
Parses the Presentation Graphic Stream (PGS) format used in Blu-ray discs.

Segment types:
  0x14 = PDS (Palette Definition Segment)
  0x15 = ODS (Object Definition Segment)
  0x16 = PCS (Presentation Composition Segment)
  0x17 = WDS (Window Definition Segment)
  0x80 = END (End of Display Set)
"""

import io
import struct
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from ..models import SubtitleFrame


PGS_MAGIC = b"PG"

SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80


@dataclass
class PgsSegment:
    pts: int  # in 90kHz ticks
    seg_type: int
    data: bytes


@dataclass
class PgsPalette:
    entries: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)  # idx -> RGBA


@dataclass
class PgsObject:
    width: int = 0
    height: int = 0
    rle_data: bytes = b""


@dataclass
class DisplaySet:
    pts: int = 0
    palette: PgsPalette | None = None
    obj: PgsObject | None = None
    is_epoch_start: bool = False


def _read_segments(data: bytes) -> list[PgsSegment]:
    """Read all PGS segments from raw .sup data."""
    segments = []
    pos = 0
    while pos + 13 <= len(data):
        # Header: "PG" (2) + PTS (4) + DTS (4) + type (1) + size (2) = 13 bytes
        if data[pos:pos + 2] != PGS_MAGIC:
            pos += 1
            continue

        pts = struct.unpack(">I", data[pos + 2:pos + 6])[0]
        seg_type = data[pos + 10]
        seg_size = struct.unpack(">H", data[pos + 11:pos + 13])[0]

        if pos + 13 + seg_size > len(data):
            break

        seg_data = data[pos + 13:pos + 13 + seg_size]
        segments.append(PgsSegment(pts=pts, seg_type=seg_type, data=seg_data))
        pos += 13 + seg_size

    return segments


def _parse_pds(data: bytes) -> PgsPalette:
    """Parse Palette Definition Segment."""
    pal = PgsPalette()
    if len(data) < 2:
        return pal
    # Skip palette_id (1) + palette_version (1)
    pos = 2
    while pos + 5 <= len(data):
        idx = data[pos]
        y = data[pos + 1]
        cr = data[pos + 2]
        cb = data[pos + 3]
        a = data[pos + 4]

        # YCrCb -> RGB
        r = max(0, min(255, int(y + 1.402 * (cr - 128))))
        g = max(0, min(255, int(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128))))
        b = max(0, min(255, int(y + 1.772 * (cb - 128))))

        pal.entries[idx] = (r, g, b, a)
        pos += 5

    return pal


def _parse_ods(data: bytes, existing: PgsObject | None = None) -> PgsObject:
    """Parse Object Definition Segment."""
    if len(data) < 4:
        return existing or PgsObject()

    # object_id (2) + version (1) + sequence_flag (1)
    seq_flag = data[3]
    is_first = (seq_flag & 0x80) != 0
    is_last = (seq_flag & 0x40) != 0

    if is_first:
        if len(data) < 11:
            return existing or PgsObject()
        # data_length (3) + width (2) + height (2)
        width = struct.unpack(">H", data[7:9])[0]
        height = struct.unpack(">H", data[9:11])[0]
        rle_data = data[11:]
        obj = PgsObject(width=width, height=height, rle_data=rle_data)
    else:
        obj = existing or PgsObject()
        obj.rle_data += data[4:]

    return obj


def _decode_rle(obj: PgsObject, palette: PgsPalette) -> Image.Image:
    """Decode RLE-compressed PGS object into an RGBA image."""
    img = Image.new("RGBA", (obj.width, obj.height), (0, 0, 0, 0))
    pixels = img.load()

    data = obj.rle_data
    pos = 0
    x = 0
    y = 0

    while pos < len(data) and y < obj.height:
        byte = data[pos]
        pos += 1

        if byte != 0:
            # Single pixel of color `byte`
            color = palette.entries.get(byte, (0, 0, 0, 0))
            if x < obj.width:
                pixels[x, y] = color
            x += 1
        else:
            if pos >= len(data):
                break
            flag = data[pos]
            pos += 1

            if flag == 0:
                # End of line
                x = 0
                y += 1
            elif flag < 0x40:
                # Short run of transparent pixels, length = flag (0x01-0x3F)
                x += flag
            elif (flag & 0xC0) == 0x40:
                # Long run of transparent pixels, 14-bit length
                if pos >= len(data):
                    break
                run_len = ((flag & 0x3F) << 8) | data[pos]
                pos += 1
                x += run_len
            elif (flag & 0xC0) == 0x80:
                # Short run of color, length in lower 6 bits
                if pos >= len(data):
                    break
                run_len = flag & 0x3F
                color_idx = data[pos]
                pos += 1
                color = palette.entries.get(color_idx, (0, 0, 0, 0))
                for _ in range(run_len):
                    if x < obj.width and y < obj.height:
                        pixels[x, y] = color
                    x += 1
            elif (flag & 0xC0) == 0xC0:
                # Long run of color, 14-bit length
                if pos >= len(data):
                    break
                run_len = ((flag & 0x3F) << 8) | data[pos]
                pos += 1
                if pos >= len(data):
                    break
                color_idx = data[pos]
                pos += 1
                color = palette.entries.get(color_idx, (0, 0, 0, 0))
                for _ in range(run_len):
                    if x < obj.width and y < obj.height:
                        pixels[x, y] = color
                    x += 1

    return img


def parse_pgs(sup_path: Path) -> list[SubtitleFrame]:
    """Parse a .sup (PGS) file into SubtitleFrames."""
    data = sup_path.read_bytes()
    segments = _read_segments(data)

    # Group segments into display sets (PCS starts a new set, END closes it)
    display_sets: list[DisplaySet] = []
    current: DisplaySet | None = None
    current_palette: PgsPalette | None = None

    for seg in segments:
        if seg.seg_type == SEG_PCS:
            current = DisplaySet(pts=seg.pts // 90)  # Convert 90kHz ticks to ms
            if len(seg.data) >= 3:
                comp_state = seg.data[7] if len(seg.data) > 7 else 0
                current.is_epoch_start = (comp_state == 0x80)
        elif seg.seg_type == SEG_PDS and current is not None:
            current_palette = _parse_pds(seg.data)
            current.palette = current_palette
        elif seg.seg_type == SEG_ODS and current is not None:
            current.obj = _parse_ods(seg.data, current.obj)
        elif seg.seg_type == SEG_END:
            if current is not None:
                if current.palette is None and current_palette is not None:
                    current.palette = current_palette
                display_sets.append(current)
            current = None

    # Convert display sets to frames
    frames = []
    frame_idx = 0

    for i, ds in enumerate(display_sets):
        if ds.obj is None or ds.obj.width == 0 or ds.obj.height == 0:
            continue
        if ds.palette is None:
            continue

        img = _decode_rle(ds.obj, ds.palette)

        # Skip blank/transparent frames (clear screen commands)
        alpha_data = img.getchannel("A")
        if alpha_data.getextrema()[1] < 10:
            # Max alpha under 10 means effectively invisible
            continue

        # Find end time
        end_ms = ds.pts + 5000
        for j in range(i + 1, len(display_sets)):
            next_pts = display_sets[j].pts
            if next_pts > ds.pts:
                end_ms = next_pts
                break

        buf = io.BytesIO()
        img.save(buf, format="PNG")

        frames.append(SubtitleFrame(
            index=frame_idx,
            start_ms=ds.pts,
            end_ms=end_ms,
            image_bytes=buf.getvalue(),
        ))
        frame_idx += 1

    return frames
