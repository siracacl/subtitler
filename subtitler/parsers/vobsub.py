"""
VobSub (DVD subtitle) parser — direct binary parsing, no ffmpeg rendering.

SubPicture decoder ported from SubtitleEdit's SubPicture.cs.
See http://www.mpucoder.com/DVD/spu.html for format spec.
"""

import io
import json
import re
import struct
import subprocess
from pathlib import Path

from PIL import Image

from ..models import SubtitleFrame


# ---------------------------------------------------------------------------
# SubPicture decoder — faithful port of SubtitleEdit SubPicture.cs
# ---------------------------------------------------------------------------

def _get_endian_word(data: bytes, index: int) -> int:
    """Read big-endian 16-bit word (equivalent to Helper.GetEndianWord)."""
    if index + 2 > len(data):
        return 0
    return (data[index] << 8) | data[index + 1]


def _decode_rle(index: int, data: bytes, only_half: bool) -> tuple[int, int, int, bool, bool]:
    """Exact port of SubtitleEdit DecodeRle.
    Returns (bytes_consumed, color, run_length, new_only_half, rest_of_line).
    """
    if index + 2 >= len(data):
        return 1, 0, 0, only_half, True

    b1 = data[index]
    b2 = data[index + 1]

    if only_half:
        b3 = data[index + 2] if index + 2 < len(data) else 0
        b1 = ((b1 & 0x0F) << 4) | ((b2 & 0xF0) >> 4)
        b2 = ((b2 & 0x0F) << 4) | ((b3 & 0xF0) >> 4)

    rest_of_line = False

    # 16-bit code: 000000nnnnnnnncc (runLength >= 64 or end-of-line when 0)
    if b1 >> 2 == 0:
        run_length = (b1 << 6) | (b2 >> 2)
        color = b2 & 0x03
        if run_length == 0:
            rest_of_line = True
            if only_half:
                return 3, color, run_length, False, rest_of_line
        return 2, color, run_length, only_half, rest_of_line

    # 8-bit code: 00nnnncc (runLength 4-15)
    if b1 >> 4 == 0:
        run_length = (b1 << 2) | (b2 >> 6)
        color = (b2 & 0x30) >> 4
        if only_half:
            return 2, color, run_length, False, rest_of_line
        return 1, color, run_length, True, rest_of_line

    # 12-bit code: 0000nnnnnncc (runLength 16-63)
    if b1 >> 6 == 0:
        run_length = b1 >> 2
        color = b1 & 0x03
        return 1, color, run_length, only_half, rest_of_line

    # 4-bit code: nncc (runLength 1-3)
    run_length = b1 >> 6
    color = (b1 & 0x30) >> 4
    if only_half:
        return 1, color, run_length, False, rest_of_line
    return 0, color, run_length, True, rest_of_line


def _generate_bitmap(data: bytes, pixels, width: int, height: int,
                     start_y: int, data_address: int,
                     four_colors: list[tuple[int, int, int, int]], add_y: int):
    """Exact port of SubtitleEdit GenerateBitmap (the RLE decompression loop)."""
    index = 0
    only_half = False
    y = start_y
    x = 0
    color_zero = four_colors[0]

    while y < height and data_address + index + 2 < len(data):
        consumed, color, run_length, only_half, rest_of_line = _decode_rle(
            data_address + index, data, only_half
        )
        index += consumed

        if rest_of_line:
            run_length = width - x

        c = four_colors[color]

        for _ in range(run_length):
            if x >= width - 1:
                if y < height and x < width and c != color_zero:
                    pixels[x, y] = c
                if only_half:
                    only_half = False
                    index += 1
                x = 0
                y += add_y
                break

            if y < height and c != color_zero:
                pixels[x, y] = c
            x += 1


def _parse_sub_picture(data: bytes, palette_16: list[tuple[int, int, int]]) -> Image.Image | None:
    """Parse a complete DVD SubPicture Unit (SPU) into an image.
    Faithful port of SubtitleEdit SubPicture.ParseDisplayControlCommands + GenerateBitmap.
    """
    if len(data) < 4:
        return None

    sub_picture_data_size = _get_endian_word(data, 0)
    start_dcsq_address = _get_endian_word(data, 2)

    # Default four colors: background, pattern, emphasis1, emphasis2
    four_colors = [(0, 0, 0, 0), (255, 255, 255, 255), (0, 0, 0, 255), (128, 128, 128, 255)]

    image_top_field = 0
    image_bottom_field = 0
    x1, y1, x2, y2 = 0, 0, 0, 0
    forced = False
    display_area_set = False

    # Walk the display control sequence table chain
    dcsq_address = start_dcsq_address
    last_dcsq_address = 0

    while (dcsq_address > last_dcsq_address and
           dcsq_address + 1 < len(data)):

        if dcsq_address + 4 >= len(data):
            break

        command_index = dcsq_address + 4
        if command_index >= len(data):
            break

        command = data[command_index]
        n_commands = 0

        while command != 0xFF and n_commands < 1000 and command_index < len(data):
            n_commands += 1

            if command == 0x00:  # ForcedStartDisplay
                forced = True
                command_index += 1
            elif command == 0x01:  # StartDisplay
                command_index += 1
            elif command == 0x02:  # StopDisplay
                command_index += 1
            elif command == 0x03:  # SetColor
                if command_index + 2 < len(data) and palette_16:
                    b1 = data[command_index + 1]
                    b2 = data[command_index + 2]
                    # Map CLUT indices to four_colors: [3]=b1>>4, [2]=b1&0xF, [1]=b2>>4, [0]=b2&0xF
                    clut_indices = [b2 & 0x0F, b2 >> 4, b1 & 0x0F, b1 >> 4]
                    for ci in range(4):
                        idx = clut_indices[ci]
                        if idx < len(palette_16):
                            r, g, b = palette_16[idx]
                            # Preserve existing alpha
                            four_colors[ci] = (r, g, b, four_colors[ci][3])
                command_index += 3
            elif command == 0x04:  # SetContrast (transparency)
                if command_index + 2 < len(data):
                    b1 = data[command_index + 1]
                    b2 = data[command_index + 2]
                    if b1 + b2 > 0:
                        # alpha: 0x0=transparent, 0xF=opaque; multiply by 17 for 0-255
                        alphas = [
                            (b2 & 0x0F) * 17,       # four_colors[0]
                            ((b2 & 0xF0) >> 4) * 17,  # four_colors[1]
                            (b1 & 0x0F) * 17,         # four_colors[2]
                            ((b1 & 0xF0) >> 4) * 17,  # four_colors[3]
                        ]
                        for ci in range(4):
                            r, g, b, _ = four_colors[ci]
                            four_colors[ci] = (r, g, b, alphas[ci])
                command_index += 3
            elif command == 0x05:  # SetDisplayArea
                if command_index + 6 < len(data) and not display_area_set:
                    x1 = (data[command_index + 1] << 8 | data[command_index + 2]) >> 4
                    x2 = (data[command_index + 2] & 0x0F) << 8 | data[command_index + 3]
                    y1 = (data[command_index + 4] << 8 | data[command_index + 5]) >> 4
                    y2 = (data[command_index + 5] & 0x0F) << 8 | data[command_index + 6]
                    display_area_set = True
                command_index += 7
            elif command == 0x06:  # SetPixelDataAddress
                if command_index + 4 < len(data):
                    image_top_field = _get_endian_word(data, command_index + 1)
                    image_bottom_field = _get_endian_word(data, command_index + 3)
                command_index += 5
            elif command == 0x07:  # ChangeColorAndContrast
                command_index += 1
                if command_index + 1 < len(data):
                    param_size = data[command_index + 1]
                    command_index += param_size
                else:
                    command_index += 1
            else:
                command_index += 1

            if command_index >= len(data):
                break
            command = data[command_index]

        last_dcsq_address = dcsq_address
        dcsq_address = _get_endian_word(data, dcsq_address + 2)

    # Generate bitmap
    width = x2 - x1 + 1
    height = y2 - y1 + 1

    if width <= 0 or height <= 0 or width > 2000 or height > 2000:
        return None

    img = Image.new("RGBA", (width, height), four_colors[0])
    pixels = img.load()

    # Decompress top field (even rows) and bottom field (odd rows)
    _generate_bitmap(data, pixels, width, height, 0, image_top_field, four_colors, 2)
    _generate_bitmap(data, pixels, width, height, 1, image_bottom_field, four_colors, 2)

    return img, forced


# ---------------------------------------------------------------------------
# MPEG-2 PS packet parsing (unchanged — this part works correctly)
# ---------------------------------------------------------------------------

PACK_START = b"\x00\x00\x01\xba"
PES_PRIVATE1 = 0xBD


def _parse_pes_private1(data: bytes, pos: int) -> tuple[int, int | None, bytes | None]:
    """Parse a PES Private Stream 1 packet, handling both MPEG-1 and MPEG-2 formats."""
    if pos + 6 > len(data):
        return len(data), None, None

    while pos + 4 <= len(data):
        if data[pos:pos + 4] == PACK_START:
            if pos + 5 > len(data):
                return len(data), None, None
            marker = (data[pos + 4] >> 6) & 0x3
            if marker == 0:
                pos += 12
            else:
                if pos + 14 > len(data):
                    return len(data), None, None
                stuffing = data[pos + 13] & 0x07
                pos += 14 + stuffing
        elif data[pos:pos + 4] == b"\x00\x00\x01\xbb":
            if pos + 6 > len(data):
                return len(data), None, None
            sys_len = struct.unpack(">H", data[pos + 4:pos + 6])[0]
            pos += 6 + sys_len
        else:
            break

    if pos + 6 > len(data) or data[pos:pos + 3] != b"\x00\x00\x01":
        return pos + 1, None, None

    stream_id = data[pos + 3]
    pkt_len = struct.unpack(">H", data[pos + 4:pos + 6])[0]

    if pos + 6 + pkt_len > len(data):
        return len(data), None, None

    next_pos = pos + 6 + pkt_len

    if stream_id != PES_PRIVATE1:
        return next_pos, None, None

    p = pos + 6
    end = pos + 6 + pkt_len
    pts = None

    while p < end and data[p] == 0xFF:
        p += 1
    if p >= end:
        return next_pos, None, None

    if (data[p] & 0xC0) == 0x40:
        p += 2
    if p >= end:
        return next_pos, None, None

    marker = (data[p] >> 4) & 0xF
    if marker == 0x2:
        if p + 5 <= end:
            pts = ((data[p] >> 1) & 0x7) << 30
            pts |= data[p + 1] << 22
            pts |= (data[p + 2] >> 1) << 15
            pts |= data[p + 3] << 7
            pts |= data[p + 4] >> 1
        p += 5
    elif marker == 0x3:
        if p + 5 <= end:
            pts = ((data[p] >> 1) & 0x7) << 30
            pts |= data[p + 1] << 22
            pts |= (data[p + 2] >> 1) << 15
            pts |= data[p + 3] << 7
            pts |= data[p + 4] >> 1
        p += 10
    elif data[p] == 0x0F:
        p += 1
    elif (data[p] & 0xC0) == 0x80:
        pes_hdr_len = data[p + 2] if p + 3 <= end else 0
        if (data[p + 1] & 0x80) and p + 3 + 5 <= end:
            hp = p + 3
            pts = ((data[hp] >> 1) & 0x7) << 30
            pts |= data[hp + 1] << 22
            pts |= (data[hp + 2] >> 1) << 15
            pts |= data[hp + 3] << 7
            pts |= data[hp + 4] >> 1
        p += 3 + pes_hdr_len

    if p >= end:
        return next_pos, None, None

    p += 1  # sub-stream ID
    payload = data[p:end]

    return next_pos, pts, payload


def _scan_all_pes_packets(data: bytes) -> list[tuple[int, bytes]]:
    """Scan .vob and group continuation packets into complete subtitle data."""
    pos = 0
    raw_packets = []

    while pos < len(data) - 6:
        idx = data.find(b"\x00\x00\x01", pos)
        if idx == -1:
            break
        next_pos, pts, payload = _parse_pes_private1(data, idx)
        if payload is not None and len(payload) > 0:
            raw_packets.append((pts, payload))
        pos = max(idx + 1, next_pos)

    subtitles = []
    current_data = bytearray()
    current_pts = None
    expected_size = 0

    for pts, payload in raw_packets:
        if pts is not None and (not current_data or len(current_data) >= expected_size):
            if current_data and expected_size > 0:
                subtitles.append((current_pts, bytes(current_data[:expected_size])))
            current_data = bytearray(payload)
            current_pts = pts
            expected_size = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 0
        else:
            current_data.extend(payload)

    if current_data and expected_size > 0:
        subtitles.append((current_pts, bytes(current_data[:expected_size])))

    return [(int(pts / 90) if pts else 0, d) for pts, d in subtitles]


# ---------------------------------------------------------------------------
# IDX palette + MKV codec private data extraction
# ---------------------------------------------------------------------------

def _parse_idx_palette(idx_text: str) -> list[tuple[int, int, int]]:
    match = re.search(r"^palette:\s*(.+)$", idx_text, re.MULTILINE)
    if not match:
        return [(i * 17, i * 17, i * 17) for i in range(16)]
    colors = []
    for hex_color in match.group(1).split(","):
        hex_color = hex_color.strip()
        if len(hex_color) >= 6:
            colors.append((int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)))
    while len(colors) < 16:
        colors.append((0, 0, 0))
    return colors[:16]


def _extract_palette_from_mkv(source_file: Path, stream_index: int) -> list[tuple[int, int, int]] | None:
    """Extract VobSub palette from MKV codec private data (idx-style text in extradata)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", str(stream_index),
        "-show_entries", "stream=extradata",
        "-show_data",
        "-of", "default=nokey=1:noprint_wrappers=1",
        str(source_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None

    text = ""
    for line in result.stdout.strip().split("\n"):
        if line.startswith("extradata=") or not line.strip():
            continue
        parts = line.split("  ")
        if len(parts) >= 2:
            hex_part = parts[0].split(": ", 1)[-1].replace(" ", "")
            try:
                text += bytes.fromhex(hex_part).decode("ascii", errors="replace")
            except ValueError:
                continue

    return _parse_idx_palette(text) if text else None


def _extract_sub_vob(source_file: Path, stream_index: int, work_dir: Path) -> Path | None:
    """Extract VobSub stream as MPEG-2 PS (.vob)."""
    vob_path = work_dir / "subs.vob"
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(source_file),
        "-map", f"0:{stream_index}",
        "-c:s", "copy", "-f", "mpeg",
        str(vob_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode == 0 and vob_path.exists() and vob_path.stat().st_size > 0:
        return vob_path
    return None


def _get_timestamps_from_ffprobe(source_file: Path, stream_index: int) -> list[tuple[int, int]]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", str(stream_index),
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "json",
        str(source_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    entries = []
    for pkt in data.get("packets", []):
        try:
            pts = float(pkt.get("pts_time", "0"))
            dur = float(pkt.get("duration_time", "0"))
        except (ValueError, TypeError):
            continue
        if pts <= 0 and dur <= 0:
            continue
        start_ms = int(pts * 1000)
        end_ms = int((pts + dur) * 1000) if dur > 0 else start_ms + 5000
        entries.append((start_ms, end_ms))
    return entries


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_vobsub_binary(
    source_file: Path,
    stream_index: int,
    work_dir: Path,
) -> list[SubtitleFrame]:
    """Parse VobSub directly from binary using SubtitleEdit's SubPicture decoder."""
    vob_path = _extract_sub_vob(source_file, stream_index, work_dir)
    if vob_path is None:
        return []

    sub_data = vob_path.read_bytes()
    packets = _scan_all_pes_packets(sub_data)
    timestamp_entries = _get_timestamps_from_ffprobe(source_file, stream_index)

    palette_16 = _extract_palette_from_mkv(source_file, stream_index)
    if not palette_16:
        palette_16 = [(i * 17, i * 17, i * 17) for i in range(16)]

    frames = []
    for i, (timestamp_ms, pkt_data) in enumerate(packets):
        if len(pkt_data) < 10:
            continue

        result = _parse_sub_picture(pkt_data, palette_16)
        if result is None:
            continue
        img, forced = result

        # Crop to non-transparent content
        bbox = img.getbbox()
        if bbox is None:
            continue

        pad = 4
        x1c = max(0, bbox[0] - pad)
        y1c = max(0, bbox[1] - pad)
        x2c = min(img.width, bbox[2] + pad)
        y2c = min(img.height, bbox[3] + pad)
        img = img.crop((x1c, y1c, x2c, y2c))

        if img.width <= 2 or img.height <= 2:
            continue

        # Flatten to RGB on black background
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img, mask=img.split()[3])

        # Find end time
        end_ms = timestamp_ms + 5000
        if timestamp_entries:
            for ts_start, ts_end in timestamp_entries:
                if abs(ts_start - timestamp_ms) < 100:
                    end_ms = ts_end
                    break
        elif i + 1 < len(packets):
            end_ms = packets[i + 1][0]

        buf = io.BytesIO()
        bg.save(buf, format="PNG")

        frames.append(SubtitleFrame(
            index=len(frames),
            start_ms=timestamp_ms,
            end_ms=end_ms,
            image_bytes=buf.getvalue(),
        ))

    vob_path.unlink(missing_ok=True)
    return frames


# ---------------------------------------------------------------------------
# Legacy ffmpeg-based functions (kept for backwards compat)
# ---------------------------------------------------------------------------

def get_entries(extracted_mkv: Path, source_file: Path, stream_index: int) -> list[tuple[int, int, float]]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "0",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "json",
        str(extracted_mkv),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    packets = json.loads(result.stdout).get("packets", []) if result.returncode == 0 else []
    if not packets:
        cmd[4] = str(stream_index)
        cmd[-1] = str(source_file)
        result = subprocess.run(cmd, capture_output=True, text=True)
        packets = json.loads(result.stdout).get("packets", []) if result.returncode == 0 else []

    entries = []
    for pkt in packets:
        try:
            pts = float(pkt.get("pts_time", "0"))
            dur = float(pkt.get("duration_time", "0"))
        except (ValueError, TypeError):
            continue
        if pts <= 0 and dur <= 0:
            continue
        start_ms = int(pts * 1000)
        end_ms = int((pts + dur) * 1000) if dur > 0 else start_ms + 5000
        entries.append((start_ms, end_ms, pts + (dur / 2 if dur > 0 else 1.0)))
    return entries


def render_overlay(source_file, stream_index, entries, output_video, res="720x480"):
    """LEGACY fallback."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type", "-of", "json", str(source_file)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
    sub_indices = [s["index"] for s in streams if s["codec_type"] == "subtitle"]
    sub_rel_idx = sub_indices.index(stream_index) if stream_index in sub_indices else 0
    max_time = max(e[2] for e in entries) + 5
    cmd = [
        "ffmpeg", "-v", "warning", "-stats", "-y",
        "-f", "lavfi", "-i", f"color=black:s={res}:d={max_time:.1f}:r=2",
        "-i", str(source_file),
        "-filter_complex", f"[0:v][1:s:{sub_rel_idx}]overlay=(W-w)/2:(H-h)/2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        str(output_video),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return proc.returncode == 0 and output_video.exists()


def extract_frames(video_path, entries, work_dir, max_workers=8, on_progress=None):
    """LEGACY fallback."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _extract_single(i, start_ms, end_ms, mid):
        png_path = work_dir / f"frame_{i:05d}.png"
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{mid:.3f}", "-i", str(video_path), "-frames:v", "1", str(png_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not png_path.exists():
            return None
        img = Image.open(png_path)
        px = list(img.getdata())
        if px and max(max(p[0], p[1], p[2]) if len(p) >= 3 else p[0] for p in px[::max(1, len(px)//3000)]) < 10:
            png_path.unlink(missing_ok=True)
            return None
        png_bytes = png_path.read_bytes()
        png_path.unlink(missing_ok=True)
        return SubtitleFrame(index=i, start_ms=start_ms, end_ms=end_ms, image_bytes=png_bytes)

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_extract_single, i, s, e, m): i for i, (s, e, m) in enumerate(entries)}
        for fut in as_completed(futures):
            idx = futures[fut]
            frame = fut.result()
            if frame is not None:
                results[idx] = frame
            if on_progress:
                on_progress(idx, frame)

    frames = []
    for i in sorted(results.keys()):
        f = results[i]
        f.index = len(frames)
        frames.append(f)
    return frames
