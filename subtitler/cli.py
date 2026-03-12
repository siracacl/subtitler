import argparse
import asyncio
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console

from .assembler import build_output_path, write_subtitles
from .config import Config, load_config
from .extractor import extract_stream
from .models import SubtitleStream, SubtitleFrame
from .ocr import OCRClient
from .probe import probe_subtitles
from .progress import console, create_progress, truncate
from .scanner import scan_videos

CODEC_LABELS = {
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VobSub",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="subtitler",
        description="OCR image-based subtitles using vision LLMs",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Video file or directory to process (default: current dir)",
    )
    p.add_argument("--config", help="Path to config YAML file")
    p.add_argument("--language", help="Only process this language (e.g. fre, eng)")
    p.add_argument("--forced-only", action="store_true", help="Only process forced subtitle tracks")
    p.add_argument("--output-format", choices=["vtt", "srt"], help="Output format")
    p.add_argument("--output-dir", help="Output directory (default: alongside video)")
    p.add_argument("--model", help="Model to use (e.g. google/gemma-3-27b-it)")
    p.add_argument("--concurrency", type=int, help="Max parallel API requests")
    p.add_argument("--api-key", help="API key (or set in config)")
    p.add_argument("--base-url", help="API base URL")
    p.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    return p.parse_args()


# --- Prepared stream: holds all data needed after rendering ---

class PreparedStream:
    def __init__(self, stream: SubtitleStream, tmp_dir: Path, out_path: Path):
        self.stream = stream
        self.tmp_dir = tmp_dir
        self.out_path = out_path
        self.frames: list[SubtitleFrame] = []
        self.rendered_video: Path | None = None
        self.entries: list = []
        self.error: str | None = None
        self.label: str = ""


def _prepare_stream(ps: PreparedStream) -> PreparedStream:
    """Extract, render overlay, and extract frames. Runs in a thread."""
    stream = ps.stream
    try:
        # Extract subtitle stream
        extracted = extract_stream(stream, ps.tmp_dir)

        if stream.is_pgs:
            from .parsers.pgs import parse_pgs
            ps.frames = parse_pgs(extracted)
        elif stream.is_vobsub:
            from .parsers.vobsub import parse_vobsub_binary
            ps.frames = parse_vobsub_binary(stream.source_file, stream.index, ps.tmp_dir)
        else:
            ps.error = f"Unsupported codec: {stream.codec}"
    except Exception as e:
        ps.error = str(e)

    return ps


async def run(config: Config) -> None:
    if not config.api_key:
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set it in subtitler.yaml, ~/.config/subtitler/config.yaml, or --api-key")
        sys.exit(1)

    root = Path(config.input_path)
    videos = scan_videos(root)

    if not videos:
        console.print(f"[yellow]No video files found in {root}[/yellow]")
        return

    console.print(f"Found [bold]{len(videos)}[/bold] video file(s)")

    all_streams: list[SubtitleStream] = []
    for v in videos:
        streams = probe_subtitles(v, config.language, config.forced_only)
        all_streams.extend(streams)

    if not all_streams:
        console.print("[yellow]No image-based subtitle streams found[/yellow]")
        return

    console.print(f"Found [bold]{len(all_streams)}[/bold] subtitle stream(s) to process:")
    for s in all_streams:
        codec_label = CODEC_LABELS.get(s.codec, s.codec)
        forced = " [forced]" if s.forced else ""
        title = f' "{s.title}"' if s.title else ""
        console.print(
            f"  {s.source_file.name} -> stream #{s.index} "
            f"[{codec_label}] {s.lang_code}{forced}{title}"
        )

    if config.dry_run:
        console.print("\n[dim]Dry run — no processing done.[/dim]")
        return

    console.print()

    # Phase 1: Prepare all streams in parallel (extract + render + frame extraction)
    console.print("[bold]Phase 1:[/bold] Extracting & rendering subtitle images...")
    tmp_dirs = []
    prepared: list[PreparedStream] = []

    for stream in all_streams:
        out_path = build_output_path(stream, config.output_format, config.output_dir)
        if out_path.exists():
            console.print(f"  [dim]Skipping {out_path.name} (already exists)[/dim]")
            continue

        codec_label = CODEC_LABELS.get(stream.codec, stream.codec)
        forced_label = " forced" if stream.forced else ""
        label = f"{stream.source_file.stem} [{codec_label} {stream.lang_code}{forced_label}]"

        tmp = tempfile.mkdtemp(prefix="subtitler_")
        tmp_dirs.append(tmp)
        ps = PreparedStream(stream, Path(tmp), out_path)
        ps.label = label
        prepared.append(ps)

    if not prepared:
        console.print("[dim]Nothing to process.[/dim]")
        return

    # Run all rendering in parallel threads
    with ThreadPoolExecutor(max_workers=min(len(prepared), 6)) as pool:
        futures = {pool.submit(_prepare_stream, ps): ps for ps in prepared}
        for fut in as_completed(futures):
            ps = futures[fut]
            try:
                fut.result()
            except Exception as e:
                ps.error = str(e)

            if ps.error:
                console.print(f"  [red]{ps.label}: {ps.error}[/red]")
            elif ps.frames:
                console.print(f"  [green]{ps.label}: {len(ps.frames)} frames ready[/green]")
            else:
                console.print(f"  [yellow]{ps.label}: no frames[/yellow]")

    # Phase 2: OCR all frames
    ready = [ps for ps in prepared if ps.frames and not ps.error]
    if not ready:
        console.print("[yellow]No frames to OCR.[/yellow]")
        return

    total_frames = sum(len(ps.frames) for ps in ready)
    console.print(f"\n[bold]Phase 2:[/bold] OCR {total_frames} frames across {len(ready)} streams...")

    ocr_client = OCRClient(config)
    try:
        progress = create_progress()
        with progress:
            for ps in ready:
                task_id = progress.add_task(ps.label, total=len(ps.frames), status="OCR...")
                completed = 0

                def on_progress(idx: int, result, _tid=task_id):
                    nonlocal completed
                    completed += 1
                    text_preview = truncate(result.text) if result.text else ""
                    progress.update(_tid, completed=completed, status=text_preview)

                results = await ocr_client.ocr_frames(ps.frames, ps.stream.language, on_progress)

                write_subtitles(results, ps.out_path, config.output_format)
                errors = sum(1 for r in results if r.text.startswith("[OCR ERROR"))
                progress.update(
                    task_id,
                    status=f"[green]Done -> {ps.out_path.name}"
                    + (f" [yellow]({errors} errors)" if errors else ""),
                )
    finally:
        await ocr_client.close()

    # Cleanup
    import shutil
    for tmp in tmp_dirs:
        shutil.rmtree(tmp, ignore_errors=True)

    console.print("\n[bold green]All done![/bold green]")


def main():
    args = parse_args()
    config = load_config(
        config_path=args.config,
        cli_overrides={
            "input_path": args.path,
            "language": args.language,
            "forced_only": args.forced_only,
            "output_format": args.output_format,
            "output_dir": args.output_dir,
            "model": args.model,
            "concurrency": args.concurrency,
            "api_key": args.api_key,
            "base_url": args.base_url,
            "dry_run": args.dry_run,
        },
    )
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
