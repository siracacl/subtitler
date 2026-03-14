"""
Web GUI for subtitler.
Run with: python -m subtitler.web
Opens a browser with drag-and-drop folder support, options, and live progress.
"""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .assembler import build_output_path, write_subtitles
from .config import Config, load_config
from .extractor import extract_stream
from .ocr import OCRClient, MultiOCRClient, ServerConfig
from .probe import probe_subtitles
from .progress import truncate
from .scanner import scan_videos

# SMB mount tracking: name -> mount_path
_smb_mounts: dict[str, Path] = {}
SMB_BASE = Path("/mnt/smb")


def _mount_smb(server: str, share: str, username: str = "", password: str = "", domain: str = "") -> tuple[str | None, Path | None]:
    """Mount an SMB share. Returns (error, mount_path)."""
    mount_name = f"{server}_{share}".replace("/", "_").replace("\\", "_")
    mount_path = SMB_BASE / mount_name

    if mount_name in _smb_mounts:
        return None, mount_path

    mount_path.mkdir(parents=True, exist_ok=True)

    unc = f"//{server}/{share}"
    opts = ["vers=3.0"]
    if username:
        opts.append(f"username={username}")
        opts.append(f"password={password}")
        if domain:
            opts.append(f"domain={domain}")
    else:
        opts.append("guest")

    cmd = ["mount", "-t", "cifs", unc, str(mount_path), "-o", ",".join(opts)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            mount_path.rmdir()
            error = result.stderr.strip() or result.stdout.strip() or "Mount failed"
            return error, None
        _smb_mounts[mount_name] = mount_path
        return None, mount_path
    except subprocess.TimeoutExpired:
        mount_path.rmdir()
        return "Connection timed out", None
    except Exception as e:
        mount_path.rmdir()
        return str(e), None


def _auto_mount_smb():
    """Auto-mount SMB share from SUBTITLER_SMB_* environment variables."""
    server = os.environ.get("SUBTITLER_SMB_SERVER", "").strip()
    share = os.environ.get("SUBTITLER_SMB_SHARE", "").strip()
    if not server or not share:
        return

    username = os.environ.get("SUBTITLER_SMB_USER", "").strip()
    password = os.environ.get("SUBTITLER_SMB_PASS", "")
    domain = os.environ.get("SUBTITLER_SMB_DOMAIN", "").strip()

    print(f"Auto-mounting SMB share //{server}/{share} ...")
    error, mount_path = _mount_smb(server, share, username, password, domain)
    if error:
        print(f"  ERROR: {error}")
    else:
        print(f"  Mounted at {mount_path}")

# Global state for SSE
_event_queues: list[queue.Queue] = []
_is_running = False
_stop_requested = False

# Progress tracking for reconnecting clients
_progress = {
    "total_streams": 0,
    "done_streams": 0,
    "current_label": "",
    "current_completed": 0,
    "current_total": 0,
    "current_text": "",
    "log": [],  # recent log messages
    "start_time": 0,
    "total_subs_done": 0,
    "total_subs_estimate": 0,  # estimated total across all streams
    "eta_seconds": 0,
    "eta_finish": "",
}


def _broadcast(event_type: str, data: dict):
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    for q in _event_queues:
        q.put(msg)


def _log(message: str):
    """Add a message to the progress log (kept to last 200 entries)."""
    _progress["log"].append(message)
    if len(_progress["log"]) > 200:
        _progress["log"] = _progress["log"][-200:]


def _reset_progress():
    _progress["total_streams"] = 0
    _progress["done_streams"] = 0
    _progress["current_label"] = ""
    _progress["current_completed"] = 0
    _progress["current_total"] = 0
    _progress["current_text"] = ""
    _progress["log"] = []
    _progress["start_time"] = time.time()
    _progress["total_subs_done"] = 0
    _progress["total_subs_estimate"] = 0
    _progress["eta_seconds"] = 0
    _progress["eta_finish"] = ""


def _update_eta():
    """Recalculate ETA based on completed subtitles and elapsed time."""
    done = _progress["total_subs_done"]
    total = _progress["total_subs_estimate"]
    if done < 1 or total < 1:
        return
    elapsed = time.time() - _progress["start_time"]
    avg_per_sub = elapsed / done
    remaining = total - done
    eta_seconds = avg_per_sub * remaining
    _progress["eta_seconds"] = eta_seconds

    from datetime import datetime, timedelta
    finish_time = datetime.now() + timedelta(seconds=eta_seconds)
    _progress["eta_finish"] = finish_time.strftime("%H:%M")

    # Format duration
    h = int(eta_seconds // 3600)
    m = int((eta_seconds % 3600) // 60)
    s = int(eta_seconds % 60)
    if h > 0:
        dur = f"{h}h {m}m"
    elif m > 0:
        dur = f"{m}m {s}s"
    else:
        dur = f"{s}s"

    _broadcast("eta", {
        "remaining": dur,
        "finish": _progress["eta_finish"],
        "done": done,
        "total": total,
        "avg": round(avg_per_sub, 2),
    })


def _prepare_one_stream(stream, tmp_dir, label, si):
    """Extract + render + extract frames for a single stream. Runs in a thread."""
    try:
        extracted = extract_stream(stream, tmp_dir)

        if stream.is_pgs:
            from .parsers.pgs import parse_pgs
            return parse_pgs(extracted), None
        elif stream.is_vobsub:
            from .parsers.vobsub import parse_vobsub_binary
            frames = parse_vobsub_binary(stream.source_file, stream.index, tmp_dir)
            return frames, None
        else:
            return [], f"Unsupported codec: {stream.codec}"
    except Exception as e:
        return [], str(e)


def _check_stop():
    if _stop_requested:
        raise _StopRequested()


class _StopRequested(Exception):
    pass


_eta_frame_counts: list[int] = []


def _run_pipeline(config: Config, servers: list[ServerConfig] | None = None):
    global _is_running, _stop_requested
    _is_running = True
    _stop_requested = False
    _reset_progress()
    _eta_frame_counts.clear()

    try:
        root = Path(config.input_path)
        videos = scan_videos(root)
        _broadcast("status", {"message": f"Found {len(videos)} video file(s)"})
        _log(f"Found {len(videos)} video file(s)")

        if not videos:
            _broadcast("done", {"message": "No video files found"})
            return

        all_streams = []
        for v in videos:
            streams = probe_subtitles(v, config.language, config.forced_only)
            all_streams.extend(streams)

        if not all_streams:
            _broadcast("done", {"message": "No image-based subtitle streams found"})
            return

        _broadcast("status", {"message": f"Found {len(all_streams)} subtitle stream(s)"})
        _log(f"Found {len(all_streams)} subtitle stream(s)")
        _progress["total_streams"] = len(all_streams)

        stream_list = []
        for s in all_streams:
            codec = "PGS" if s.is_pgs else "VobSub" if s.is_vobsub else s.codec
            forced = " forced" if s.forced else ""
            stream_list.append(f"{s.source_file.name} [{codec}] {s.lang_code}{forced}")
        _broadcast("streams", {"streams": stream_list})

        import tempfile
        import shutil
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Group streams by video
        from collections import defaultdict
        video_streams: dict[Path, list[tuple[int, object]]] = defaultdict(list)
        for si, stream in enumerate(all_streams):
            video_streams[stream.source_file].append((si, stream))

        loop = asyncio.new_event_loop()
        if servers:
            def on_server_fail(name, error):
                msg = f"Server '{name}' failed: {error}. Redistributing work to remaining servers."
                _broadcast("status", {"message": msg})
                _log(msg)

            ocr_client = MultiOCRClient(
                config, servers,
                stop_check=lambda: _stop_requested,
                on_server_fail=on_server_fail,
            )
            server_names = ", ".join(s.name for s in servers)
            total_concurrency = sum(s.concurrency for s in servers)
            _broadcast("status", {"message": f"Using {len(servers)} server(s): {server_names} (total concurrency: {total_concurrency})"})
            _log(f"Using {len(servers)} server(s): {server_names}")
        else:
            ocr_client = OCRClient(config=config, stop_check=lambda: _stop_requested)
        any_work = False

        try:
            for vi, (video_path, streams) in enumerate(video_streams.items()):
                _check_stop()
                _broadcast("status", {
                    "message": f"Video {vi+1}/{len(video_streams)}: {video_path.name}",
                })

                # Build work items for this video (skip existing)
                work_items = []
                for si, stream in streams:
                    codec = "PGS" if stream.is_pgs else "VobSub"
                    label = f"{stream.source_file.stem} [{codec} {stream.lang_code}]"
                    out_path = build_output_path(stream, config.output_format, config.output_dir)

                    if out_path.exists():
                        _broadcast("stream_skip", {
                            "stream_idx": si, "label": label,
                            "reason": f"{out_path.name} already exists",
                        })
                        continue

                    tmp_dir = Path(tempfile.mkdtemp(prefix="subtitler_"))
                    work_items.append((si, stream, label, out_path, tmp_dir))

                if not work_items:
                    continue

                any_work = True

                # Extract streams in background, OCR as soon as each is ready
                extract_pool = ThreadPoolExecutor(max_workers=min(len(work_items), 6))
                futures = {}
                for si, stream, label, out_path, tmp_dir in work_items:
                    _broadcast("stream_start", {
                        "stream_idx": si, "label": label,
                        "total_streams": len(all_streams),
                    })
                    _broadcast("stream_phase", {"stream_idx": si, "phase": "Extracting..."})
                    fut = extract_pool.submit(_prepare_one_stream, stream, tmp_dir, label, si)
                    futures[fut] = (si, stream, label, out_path, tmp_dir)

                # Process each stream as soon as its extraction completes
                for fut in as_completed(futures):
                    si, stream, label, out_path, tmp_dir = futures[fut]
                    frames, error = fut.result()

                    if error:
                        _broadcast("stream_error", {"stream_idx": si, "error": error})
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        _progress["done_streams"] += 1
                        continue
                    if not frames:
                        _broadcast("stream_error", {"stream_idx": si, "error": "No frames found"})
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        _progress["done_streams"] += 1
                        continue

                    _broadcast("stream_phase", {
                        "stream_idx": si,
                        "phase": f"{len(frames)} frames ready, starting OCR...",
                    })

                    # Track frame counts for ETA estimation
                    _eta_frame_counts.append(len(frames))
                    avg_frames = sum(_eta_frame_counts) / len(_eta_frame_counts)
                    remaining_streams = max(0, _progress["total_streams"] - _progress["done_streams"] - 1)
                    _progress["total_subs_estimate"] = int(
                        _progress["total_subs_done"] + len(frames) +
                        remaining_streams * avg_frames
                    )

                    _check_stop()

                    codec = "PGS" if stream.is_pgs else "VobSub"
                    label = f"{stream.source_file.stem} [{codec} {stream.lang_code}]"

                    _broadcast("stream_ocr_start", {
                        "stream_idx": si,
                        "total_frames": len(frames),
                    })
                    _progress["current_label"] = label
                    _progress["current_completed"] = 0
                    _progress["current_total"] = len(frames)
                    _progress["current_text"] = ""

                    completed = 0

                    def on_progress(idx, result, _si=si, _total=len(frames)):
                        nonlocal completed
                        completed += 1
                        _progress["total_subs_done"] += 1
                        txt = truncate(result.text, 80) if result.text else ""
                        _progress["current_completed"] = completed
                        _progress["current_text"] = txt
                        _broadcast("stream_ocr_progress", {
                            "stream_idx": _si,
                            "completed": completed,
                            "total": _total,
                            "text": txt,
                        })
                        # Update ETA every 5 subtitles to avoid spamming
                        if _progress["total_subs_done"] % 5 == 0 or completed == _total:
                            _update_eta()

                    results = loop.run_until_complete(
                        ocr_client.ocr_frames(frames, stream.language, on_progress)
                    )

                    _check_stop()

                    # Filter out stopped results
                    results = [r for r in results if r.text != "[STOPPED]"]
                    write_subtitles(results, out_path, config.output_format)
                    errors = sum(1 for r in results if r.text.startswith("[OCR ERROR"))
                    _progress["done_streams"] += 1
                    _log(f"Done: {out_path.name} ({len(frames)} frames, {errors} errors)")
                    _broadcast("stream_done", {
                        "stream_idx": si,
                        "output": out_path.name,
                        "total_frames": len(frames),
                        "errors": errors,
                    })

                    shutil.rmtree(tmp_dir, ignore_errors=True)

                extract_pool.shutdown(wait=False)

        finally:
            loop.run_until_complete(ocr_client.close())
            loop.close()

        if not any_work:
            _broadcast("done", {"message": "All outputs already exist"})
        else:
            _broadcast("done", {"message": "All done!"})

    except _StopRequested:
        _broadcast("done", {"message": "Stopped by user"})
    except Exception as e:
        _broadcast("done", {"message": f"Error: {e}"})
    finally:
        _is_running = False
        _stop_requested = False


class GUIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress request logs

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_HTML.encode())

        elif parsed.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue()
            _event_queues.append(q)
            try:
                while True:
                    try:
                        msg = q.get(timeout=30)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Send keepalive
                        self.wfile.write(": keepalive\n\n".encode())
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                _event_queues.remove(q)

        elif parsed.path == "/status":
            self._json_response({
                "running": _is_running,
                "stopping": _stop_requested,
                "progress": _progress if _is_running else None,
            })

        elif parsed.path == "/browse":
            qs = parse_qs(parsed.query)
            browse_path = unquote(qs.get("path", ["/"])[0])
            self._handle_browse(browse_path)

        elif parsed.path == "/smb/list":
            self._json_response({
                "mounts": [
                    {"name": name, "path": str(path)}
                    for name, path in _smb_mounts.items()
                ]
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global _stop_requested
        parsed = urlparse(self.path)

        if parsed.path == "/scan":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            folder = unquote(body.get("folder", ".")).strip()

            root = Path(folder)
            if not root.exists():
                self._json_response({"error": f"Path not found: {folder}"}, 400)
                return

            videos = scan_videos(root)
            all_streams = []
            for v in videos:
                streams = probe_subtitles(v)
                all_streams.extend(streams)

            stream_info = []
            for s in all_streams:
                codec = "PGS" if s.is_pgs else "VobSub" if s.is_vobsub else s.codec
                stream_info.append({
                    "file": s.source_file.name,
                    "codec": codec,
                    "language": s.lang_code,
                    "forced": s.forced,
                    "index": s.index,
                })

            # Collect unique languages
            languages = sorted(set(s.lang_code for s in all_streams))

            self._json_response({
                "videos": len(videos),
                "streams": len(all_streams),
                "stream_info": stream_info,
                "languages": languages,
            })

        elif parsed.path == "/start":
            if _is_running and not _stop_requested:
                self._json_response({"error": "Already running"}, 409)
                return
            # Wait briefly for a stopping pipeline to finish
            if _is_running and _stop_requested:
                for _ in range(50):
                    import time; time.sleep(0.1)
                    if not _is_running:
                        break

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            # Parse server configs from request
            servers = None
            raw_servers = body.get("servers")
            if raw_servers and isinstance(raw_servers, list) and len(raw_servers) > 0:
                servers = [
                    ServerConfig(
                        name=s.get("name", f"Server {i+1}"),
                        base_url=s["base_url"],
                        api_key=s.get("api_key", ""),
                        model=s["model"],
                        concurrency=int(s.get("concurrency", 4)),
                    )
                    for i, s in enumerate(raw_servers)
                    if s.get("base_url") and s.get("model")
                ]
                if not servers:
                    servers = None

            config = load_config(cli_overrides={
                "input_path": unquote(body.get("folder", ".")).strip(),
                "language": body.get("language") or None,
                "forced_only": body.get("forced_only", False),
                "output_format": body.get("output_format", "vtt"),
            })

            thread = threading.Thread(target=_run_pipeline, args=(config, servers), daemon=True)
            thread.start()

            self._json_response({"status": "started"})

        elif parsed.path == "/estimate":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            self._handle_estimate(body)

        elif parsed.path == "/stop":
            if _is_running:
                _stop_requested = True
                self._json_response({"status": "stopping"})
            else:
                self._json_response({"status": "not_running"})

        elif parsed.path == "/smb/connect":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            self._handle_smb_connect(body)

        elif parsed.path == "/smb/disconnect":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            self._handle_smb_disconnect(body)

        else:
            self.send_response(404)
            self.end_headers()

    def _handle_browse(self, browse_path: str):
        """List files and directories at the given path."""
        p = Path(browse_path)
        if not p.exists() or not p.is_dir():
            self._json_response({"error": f"Not a directory: {browse_path}"}, 400)
            return

        VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".wmv", ".mov"}
        entries = []
        try:
            for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    entries.append({"name": item.name, "type": "dir", "path": str(item)})
                elif item.suffix.lower() in VIDEO_EXTS:
                    size_mb = item.stat().st_size / (1024 * 1024)
                    entries.append({
                        "name": item.name, "type": "file",
                        "path": str(item), "size": f"{size_mb:.0f} MB",
                    })
        except PermissionError:
            self._json_response({"error": f"Permission denied: {browse_path}"}, 403)
            return

        self._json_response({
            "path": str(p),
            "parent": str(p.parent) if p != p.parent else None,
            "entries": entries,
        })

    def _handle_smb_connect(self, body: dict):
        """Mount an SMB share inside the container."""
        server = body.get("server", "").strip()
        share = body.get("share", "").strip()
        username = body.get("username", "").strip()
        password = body.get("password", "")
        domain = body.get("domain", "").strip()

        if not server or not share:
            self._json_response({"error": "Server and share are required"}, 400)
            return

        mount_name = f"{server}_{share}".replace("/", "_").replace("\\", "_")
        error, mount_path = _mount_smb(server, share, username, password, domain)

        if error:
            self._json_response({"error": error}, 500)
        elif mount_name in _smb_mounts and mount_path:
            self._json_response({
                "status": "mounted",
                "path": str(mount_path),
                "name": mount_name,
            })

    def _handle_smb_disconnect(self, body: dict):
        """Unmount an SMB share."""
        name = body.get("name", "").strip()
        if name not in _smb_mounts:
            self._json_response({"error": f"Not mounted: {name}"}, 404)
            return

        mount_path = _smb_mounts[name]
        try:
            subprocess.run(["umount", str(mount_path)], capture_output=True, timeout=10)
            mount_path.rmdir()
            del _smb_mounts[name]
            self._json_response({"status": "disconnected"})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_estimate(self, body: dict):
        """Benchmark OCR speed with 10 sample subtitles and estimate total time."""
        import random
        import tempfile
        import shutil

        folder = unquote(body.get("folder", ".")).strip()
        subs_per_file = int(body.get("subs_per_stream", 400))

        # Parse servers for concurrency calculation
        raw_servers = body.get("servers")
        servers = []
        if raw_servers and isinstance(raw_servers, list):
            servers = [
                ServerConfig(
                    name=s.get("name", f"Server {i+1}"),
                    base_url=s["base_url"],
                    api_key=s.get("api_key", ""),
                    model=s["model"],
                    concurrency=int(s.get("concurrency", 4)),
                )
                for i, s in enumerate(raw_servers)
                if s.get("base_url") and s.get("model")
            ]
        total_concurrency = sum(s.concurrency for s in servers) if servers else int(body.get("concurrency", 0)) or 10

        root = Path(folder)
        if not root.exists():
            self._json_response({"error": f"Path not found: {folder}"}, 400)
            return

        # Use first server for benchmark if available
        estimate_server = servers[0] if servers else None
        config = load_config(cli_overrides={
            "input_path": folder,
            "language": body.get("language") or None,
            "forced_only": body.get("forced_only", False),
            "concurrency": 1,  # sequential for accurate timing
        })

        # Find first stream with subtitle frames
        videos = scan_videos(root)
        if not videos:
            self._json_response({"error": "No video files found"}, 400)
            return

        all_streams = []
        for v in videos:
            streams = probe_subtitles(v, config.language, config.forced_only)
            all_streams.extend(streams)

        if not all_streams:
            self._json_response({"error": "No subtitle streams found"}, 400)
            return

        total_files = len(videos)
        total_streams = len(all_streams)

        # Extract frames from the first stream
        stream = all_streams[0]
        tmp_dir = Path(tempfile.mkdtemp(prefix="subtitler_est_"))
        try:
            extracted = extract_stream(stream, tmp_dir)
            if stream.is_pgs:
                from .parsers.pgs import parse_pgs
                frames = parse_pgs(extracted)
            elif stream.is_vobsub:
                from .parsers.vobsub import parse_vobsub_binary
                frames = parse_vobsub_binary(stream.source_file, stream.index, tmp_dir)
            else:
                self._json_response({"error": f"Unsupported codec: {stream.codec}"}, 400)
                return

            if not frames:
                self._json_response({"error": "No frames found in first stream"}, 400)
                return

            # Pick up to 10 random samples
            sample_count = min(10, len(frames))
            samples = random.sample(frames, sample_count)

            # Benchmark OCR (use first server if available, else config)
            loop = asyncio.new_event_loop()
            if estimate_server:
                ocr_client = OCRClient(config=config, server=estimate_server)
            else:
                ocr_client = OCRClient(config=config)
            times = []

            try:
                for frame in samples:
                    t0 = time.time()
                    loop.run_until_complete(ocr_client.ocr_frame(frame, stream.language))
                    times.append(time.time() - t0)
            finally:
                loop.run_until_complete(ocr_client.close())
                loop.close()

            avg_time = sum(times) / len(times)
            total_subs = subs_per_file * total_streams
            # With concurrency, effective time = total / concurrency
            effective_concurrency = min(total_concurrency, total_subs)
            total_seconds = (avg_time * total_subs) / effective_concurrency

            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            secs = int(total_seconds % 60)
            if hours > 0:
                time_str = f"{hours}h {minutes}m {secs}s"
            elif minutes > 0:
                time_str = f"{minutes}m {secs}s"
            else:
                time_str = f"{secs}s"

            self._json_response({
                "avg_seconds_per_sub": round(avg_time, 2),
                "sample_count": sample_count,
                "sample_times": [round(t, 2) for t in times],
                "total_files": total_files,
                "total_streams": total_streams,
                "subs_per_stream": subs_per_file,
                "total_subs_estimate": total_subs,
                "concurrency": total_concurrency,
                "num_servers": len(servers) if servers else 1,
                "total_time_estimate": time_str,
                "total_seconds": round(total_seconds, 1),
                "first_stream_actual_frames": len(frames),
            })
        except Exception as e:
            self._json_response({"error": str(e)}, 500)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Subtitler</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f0f0f;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 2rem;
  }
  h1 { font-size: 1.8rem; margin-bottom: 0.5rem; color: #fff; }
  .subtitle { color: #888; margin-bottom: 2rem; font-size: 0.9rem; }
  .container { max-width: 900px; margin: 0 auto; }

  /* SMB panel */
  .smb-panel {
    background: #1a1a1a;
    border: 1px solid #222;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
  }
  .smb-panel h3 {
    font-size: 0.95rem;
    color: #ccc;
    margin-bottom: 1rem;
  }
  .smb-form {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.8rem;
  }
  .smb-form .full { grid-column: span 2; }
  .smb-form input {
    width: 100%;
    padding: 0.6rem 0.8rem;
    background: #0f0f0f;
    border: 1px solid #333;
    border-radius: 6px;
    color: #e0e0e0;
    font-size: 0.9rem;
  }
  .smb-form input:focus { outline: none; border-color: #4a9eff; }
  .smb-form input::placeholder { color: #555; }
  .smb-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.8rem;
    grid-column: span 2;
  }
  .smb-mounts {
    margin-top: 1rem;
    font-size: 0.85rem;
  }
  .smb-mount-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 0.8rem;
    background: #0f0f0f;
    border-radius: 6px;
    margin-top: 0.4rem;
  }
  .smb-mount-item .mount-path { color: #4aff9e; font-family: 'SF Mono', Monaco, monospace; font-size: 0.8rem; }
  .smb-mount-item .mount-name { color: #ccc; }
  .btn-disconnect {
    background: #3a2020;
    color: #ff6666;
    border: none;
    padding: 0.3rem 0.8rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
  }
  .btn-disconnect:hover { background: #4a2020; }
  .smb-error { color: #ff4a4a; font-size: 0.85rem; margin-top: 0.5rem; }
  .smb-status { color: #4aff9e; font-size: 0.85rem; margin-top: 0.5rem; }

  /* Folder input */
  .folder-input {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1.5rem;
  }
  .folder-input input {
    flex: 1;
    padding: 0.7rem 1rem;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 8px;
    color: #e0e0e0;
    font-size: 0.95rem;
  }
  .folder-input input:focus { outline: none; border-color: #4a9eff; }

  /* Options */
  .options {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-bottom: 1.5rem;
    background: #1a1a1a;
    padding: 1.5rem;
    border-radius: 12px;
    border: 1px solid #222;
  }
  .option label {
    display: block;
    font-size: 0.8rem;
    color: #888;
    margin-bottom: 0.3rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .option select, .option input[type="text"], .option input[type="number"] {
    width: 100%;
    padding: 0.6rem 0.8rem;
    background: #0f0f0f;
    border: 1px solid #333;
    border-radius: 6px;
    color: #e0e0e0;
    font-size: 0.9rem;
  }
  .option select[multiple] { min-height: 4.5rem; }
  .option select[multiple] option { padding: 0.3rem 0.5rem; }
  .option select:focus, .option input:focus { outline: none; border-color: #4a9eff; }
  .checkbox-option {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding-top: 1.2rem;
  }
  .checkbox-option input[type="checkbox"] {
    width: 18px;
    height: 18px;
    accent-color: #4a9eff;
  }

  /* Scan info */
  .scan-info {
    background: #1a1a1a;
    border: 1px solid #222;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1.5rem;
    display: none;
  }
  .scan-info.visible { display: block; }
  .scan-info .summary { font-size: 0.95rem; margin-bottom: 0.8rem; color: #ccc; }
  .scan-info .stream-list {
    max-height: 200px;
    overflow-y: auto;
    font-family: 'SF Mono', Monaco, monospace;
    font-size: 0.8rem;
    color: #888;
    line-height: 1.6;
  }
  .stream-list .codec { color: #4a9eff; }
  .stream-list .lang { color: #4aff9e; }
  .stream-list .forced { color: #ff9e4a; }

  /* Buttons */
  .buttons { display: flex; gap: 1rem; margin-bottom: 2rem; }
  button {
    padding: 0.8rem 2rem;
    border: none;
    border-radius: 8px;
    font-size: 1rem;
    cursor: pointer;
    font-weight: 600;
    transition: all 0.15s;
  }
  .btn-scan { background: #2a2a2a; color: #ccc; }
  .btn-scan:hover { background: #333; }
  .btn-browse { background: #2a2a3a; color: #aac; }
  .btn-browse:hover { background: #333; }
  .btn-start { background: #4a9eff; color: #fff; }
  .btn-start:hover { background: #3a8eef; }
  .btn-start:disabled { background: #333; color: #666; cursor: not-allowed; }
  .btn-stop { background: #ff4a4a; color: #fff; }
  .btn-stop:hover { background: #e03a3a; }
  .btn-clear { background: #2a2a2a; color: #888; }
  .btn-clear:hover { background: #333; color: #ccc; }
  .btn-smb { background: #2a3a2a; color: #afc; }
  .btn-smb:hover { background: #3a4a3a; }
  .btn-estimate { background: #2a2a3a; color: #aac; }
  .btn-estimate:hover { background: #3a3a4a; }
  .btn-estimate:disabled { background: #222; color: #555; cursor: not-allowed; }

  /* Servers panel */
  .servers-panel {
    background: #1a1a1a;
    border: 1px solid #222;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
  }
  .servers-panel h3 {
    font-size: 0.95rem;
    color: #ccc;
    margin-bottom: 1rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .server-list { display: flex; flex-direction: column; gap: 0.8rem; }
  .server-item {
    background: #0f0f0f;
    border: 1px solid #333;
    border-radius: 8px;
    padding: 1rem;
  }
  .server-item.disabled { opacity: 0.5; }
  .server-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.8rem;
  }
  .server-header .server-name {
    font-weight: 600;
    color: #e0e0e0;
    font-size: 0.95rem;
  }
  .server-header .server-actions {
    display: flex;
    gap: 0.4rem;
    align-items: center;
  }
  .server-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.6rem;
  }
  .server-fields .full { grid-column: span 2; }
  .server-fields label {
    display: block;
    font-size: 0.7rem;
    color: #666;
    margin-bottom: 0.2rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .server-fields input {
    width: 100%;
    padding: 0.5rem 0.7rem;
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 5px;
    color: #e0e0e0;
    font-size: 0.85rem;
  }
  .server-fields input:focus { outline: none; border-color: #4a9eff; }
  .server-fields input::placeholder { color: #444; }
  .btn-add-server {
    background: #2a3a2a;
    color: #afc;
    border: none;
    padding: 0.4rem 1rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .btn-add-server:hover { background: #3a4a3a; }
  .btn-remove-server {
    background: #3a2020;
    color: #ff6666;
    border: none;
    padding: 0.25rem 0.6rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
  }
  .btn-remove-server:hover { background: #4a2020; }
  .server-toggle {
    width: 16px;
    height: 16px;
    accent-color: #4a9eff;
  }

  /* Estimate panel */
  .estimate-panel {
    background: #1a1a1a;
    border: 1px solid #222;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1.5rem;
  }
  .estimate-panel h3 {
    font-size: 0.95rem;
    color: #ccc;
    margin-bottom: 1rem;
  }
  .estimate-form {
    display: flex;
    gap: 0.8rem;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .estimate-field label {
    display: block;
    font-size: 0.75rem;
    color: #888;
    margin-bottom: 0.3rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .estimate-field input {
    width: 120px;
    padding: 0.6rem 0.8rem;
    background: #0f0f0f;
    border: 1px solid #333;
    border-radius: 6px;
    color: #e0e0e0;
    font-size: 0.9rem;
  }
  .estimate-field input:focus { outline: none; border-color: #4a9eff; }
  .estimate-result {
    margin-top: 1rem;
    font-size: 0.9rem;
    color: #ccc;
    display: none;
  }
  .estimate-result.visible { display: block; }
  .estimate-result .est-time {
    font-size: 1.3rem;
    font-weight: 700;
    color: #4a9eff;
    margin: 0.5rem 0;
  }
  .estimate-result .est-details {
    font-size: 0.8rem;
    color: #888;
    font-family: 'SF Mono', Monaco, monospace;
    line-height: 1.6;
  }

  /* Progress area */
  .progress-area { display: none; }
  .progress-area.visible { display: block; }
  .progress-area h2 { font-size: 1.1rem; margin-bottom: 1rem; color: #fff; }

  .log {
    background: #1a1a1a;
    border: 1px solid #222;
    border-radius: 12px;
    padding: 1.5rem;
    max-height: 500px;
    overflow-y: auto;
    font-family: 'SF Mono', Monaco, monospace;
    font-size: 0.85rem;
    line-height: 1.8;
  }
  .log-entry { padding: 0.2rem 0; }
  .log-entry.status { color: #888; }
  .log-entry.phase { color: #4a9eff; }
  .log-entry.ocr { color: #ccc; }
  .log-entry.done { color: #4aff9e; }
  .log-entry.error { color: #ff4a4a; }
  .log-entry.skip { color: #666; }

  /* Overall progress bar */
  .overall-progress { margin-bottom: 1rem; }
  .overall-progress .info {
    display: flex;
    justify-content: space-between;
    margin-bottom: 0.4rem;
    font-size: 0.85rem;
  }
  .overall-progress .info .label { color: #ccc; }
  .overall-progress .info .count { color: #888; }
  .overall-progress .progress-bar .fill { background: #4aff9e; }

  /* ETA display */
  .eta-display {
    display: none;
    margin-bottom: 1rem;
    padding: 0.6rem 1rem;
    background: #1a2a1a;
    border: 1px solid #2a3a2a;
    border-radius: 8px;
    font-size: 0.9rem;
    color: #ccc;
  }
  .eta-display.visible { display: flex; align-items: center; gap: 0.6rem; }
  .eta-remaining { color: #4aff9e; font-weight: 700; }
  .eta-separator { color: #444; }
  .eta-finish { color: #4a9eff; font-weight: 600; }
  .eta-detail { color: #666; font-size: 0.8rem; margin-left: auto; font-family: 'SF Mono', Monaco, monospace; }

  /* Current progress bar */
  .current-progress { margin-bottom: 1rem; display: none; }
  .current-progress.visible { display: block; }
  .current-progress .info {
    display: flex;
    justify-content: space-between;
    margin-bottom: 0.4rem;
    font-size: 0.85rem;
  }
  .current-progress .info .label { color: #ccc; }
  .current-progress .info .count { color: #888; }
  .progress-bar {
    height: 6px;
    background: #222;
    border-radius: 3px;
    overflow: hidden;
  }
  .progress-bar .fill {
    height: 100%;
    background: #4a9eff;
    border-radius: 3px;
    transition: width 0.2s;
    width: 0%;
  }
  .current-text {
    margin-top: 0.4rem;
    font-size: 0.8rem;
    color: #666;
    font-family: 'SF Mono', Monaco, monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* File browser modal */
  .modal-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7);
    z-index: 100;
    justify-content: center;
    align-items: center;
  }
  .modal-overlay.visible { display: flex; }
  .modal {
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 12px;
    width: 700px;
    max-width: 90vw;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
  }
  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #333;
  }
  .modal-header h3 { color: #fff; font-size: 1rem; }
  .modal-close {
    background: none;
    border: none;
    color: #888;
    font-size: 1.5rem;
    cursor: pointer;
    padding: 0 0.5rem;
  }
  .modal-close:hover { color: #fff; }
  .modal-breadcrumb {
    padding: 0.8rem 1.5rem;
    background: #0f0f0f;
    font-family: 'SF Mono', Monaco, monospace;
    font-size: 0.8rem;
    color: #888;
    border-bottom: 1px solid #222;
    display: flex;
    align-items: center;
    gap: 0.3rem;
    flex-wrap: wrap;
  }
  .modal-breadcrumb .crumb {
    color: #4a9eff;
    cursor: pointer;
    padding: 0.1rem 0.2rem;
    border-radius: 3px;
  }
  .modal-breadcrumb .crumb:hover { background: #2a2a3a; }
  .modal-breadcrumb .sep { color: #444; }
  .modal-body {
    flex: 1;
    overflow-y: auto;
    padding: 0;
  }
  .file-list { list-style: none; }
  .file-item {
    display: flex;
    align-items: center;
    padding: 0.6rem 1.5rem;
    cursor: pointer;
    border-bottom: 1px solid #1f1f1f;
    gap: 0.8rem;
  }
  .file-item:hover { background: #222; }
  .file-item.selected { background: #1a2a3a; }
  .file-icon { font-size: 1.1rem; width: 1.5rem; text-align: center; }
  .file-name { flex: 1; font-size: 0.9rem; }
  .file-size { color: #666; font-size: 0.8rem; font-family: 'SF Mono', Monaco, monospace; }
  .file-item.dir .file-name { color: #4a9eff; }
  .file-item.file .file-name { color: #ccc; }
  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.8rem;
    padding: 1rem 1.5rem;
    border-top: 1px solid #333;
  }
  .modal-footer button { padding: 0.6rem 1.5rem; font-size: 0.9rem; }
  .btn-select-folder { background: #4a9eff; color: #fff; }
  .btn-select-folder:hover { background: #3a8eef; }
  .btn-cancel { background: #2a2a2a; color: #ccc; }
  .btn-cancel:hover { background: #333; }
  .browse-loading {
    padding: 2rem;
    text-align: center;
    color: #666;
  }
  .browse-empty {
    padding: 2rem;
    text-align: center;
    color: #555;
    font-size: 0.9rem;
  }
</style>
</head>
<body>
<div class="container">
  <h1>Subtitler</h1>
  <p class="subtitle">OCR image-based subtitles using vision LLMs</p>

  <!-- SMB Connection Panel -->
  <div class="smb-panel">
    <h3>Network Share (SMB/CIFS)</h3>
    <div class="smb-form">
      <input type="text" id="smbServer" placeholder="Server (IP or hostname)" />
      <input type="text" id="smbShare" placeholder="Share name" />
      <input type="text" id="smbUser" placeholder="Username (optional)" />
      <input type="password" id="smbPass" placeholder="Password" />
      <input type="text" id="smbDomain" placeholder="Domain (optional)" class="full" />
      <div class="smb-actions">
        <button class="btn-smb" onclick="connectSmb()">Connect</button>
      </div>
    </div>
    <div id="smbError" class="smb-error"></div>
    <div id="smbStatus" class="smb-status"></div>
    <div class="smb-mounts" id="smbMounts"></div>
  </div>

  <!-- Folder input with browse button -->
  <div class="folder-input">
    <input type="text" id="folderPath" placeholder="Enter folder or video path..." />
    <button class="btn-browse" onclick="openBrowser()">Browse</button>
    <button class="btn-scan" onclick="scanFolder()">Scan</button>
  </div>

  <div class="scan-info" id="scanInfo">
    <div class="summary" id="scanSummary"></div>
    <div class="stream-list" id="streamList"></div>
  </div>

  <div class="options">
    <div class="option">
      <label>Languages (none selected = all)</label>
      <select id="optLanguage" multiple></select>
    </div>
    <div class="option">
      <label>Output Format</label>
      <select id="optFormat">
        <option value="vtt">VTT</option>
        <option value="srt">SRT</option>
      </select>
    </div>
    <div class="option checkbox-option" style="grid-column: span 2;">
      <input type="checkbox" id="optForced" />
      <label style="margin: 0; text-transform: none; font-size: 0.95rem; color: #ccc;">
        Forced subtitles only
      </label>
    </div>
  </div>

  <!-- Servers Panel -->
  <div class="servers-panel">
    <h3>
      <span>API Servers</span>
      <button class="btn-add-server" onclick="addServer()">+ Add Server</button>
    </h3>
    <div class="server-list" id="serverList"></div>
  </div>

  <!-- Time Estimate -->
  <div class="estimate-panel">
    <h3>Time Estimate</h3>
    <div class="estimate-form">
      <div class="estimate-field">
        <label>Subtitles per stream</label>
        <input type="number" id="estSubsPerFile" value="400" min="1" />
      </div>
      <button class="btn-estimate" id="btnEstimate" onclick="runEstimate()" disabled>
        Estimate
      </button>
    </div>
    <div class="estimate-result" id="estimateResult">
      <div class="est-time" id="estTime"></div>
      <div class="est-details" id="estDetails"></div>
    </div>
  </div>

  <div class="buttons">
    <button class="btn-start" id="btnStart" onclick="startProcessing()" disabled>
      Start OCR
    </button>
    <button class="btn-stop" id="btnStop" onclick="stopProcessing()" style="display:none;">
      Stop
    </button>
    <button class="btn-clear" onclick="clearLog()">Clear Log</button>
  </div>

  <div class="progress-area" id="progressArea">
    <h2>Progress</h2>
    <div class="overall-progress" id="overallProgress">
      <div class="info">
        <span class="label">Overall</span>
        <span class="count" id="opCount">0/0 streams</span>
      </div>
      <div class="progress-bar"><div class="fill" id="opFill"></div></div>
    </div>
    <div class="eta-display" id="etaDisplay">
      <span class="eta-remaining" id="etaRemaining"></span>
      <span class="eta-separator">-</span>
      <span class="eta-finish" id="etaFinish"></span>
      <span class="eta-detail" id="etaDetail"></span>
    </div>
    <div class="current-progress" id="currentProgress">
      <div class="info">
        <span class="label" id="cpLabel">...</span>
        <span class="count" id="cpCount">0/0</span>
      </div>
      <div class="progress-bar"><div class="fill" id="cpFill"></div></div>
      <div class="current-text" id="cpText"></div>
    </div>
    <div class="log" id="log"></div>
  </div>
</div>

<!-- File Browser Modal -->
<div class="modal-overlay" id="browserModal">
  <div class="modal">
    <div class="modal-header">
      <h3>Browse Files</h3>
      <button class="modal-close" onclick="closeBrowser()">&times;</button>
    </div>
    <div class="modal-breadcrumb" id="browserBreadcrumb"></div>
    <div class="modal-body" id="browserBody">
      <div class="browse-loading">Loading...</div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeBrowser()">Cancel</button>
      <button class="btn-select-folder" id="btnSelectFolder" onclick="selectBrowsed()">
        Select this folder
      </button>
    </div>
  </div>
</div>

<script>
let eventSource = null;
let scannedFolder = null;
let currentBrowsePath = '/';

/* --- Servers --- */

function getServers() {
  try {
    return JSON.parse(localStorage.getItem('subtitler_servers') || '[]');
  } catch { return []; }
}

function saveServers(servers) {
  localStorage.setItem('subtitler_servers', JSON.stringify(servers));
}

function getEnabledServers() {
  return getServers().filter(s => s.enabled !== false);
}

function getTotalConcurrency() {
  const enabled = getEnabledServers();
  if (!enabled.length) return 10;
  return enabled.reduce((sum, s) => sum + (parseInt(s.concurrency) || 4), 0);
}

function renderServers() {
  const servers = getServers();
  const container = document.getElementById('serverList');

  if (!servers.length) {
    container.innerHTML = '<div style="color:#555;font-size:0.85rem;padding:0.5rem 0;">No servers configured. Add a server to get started.</div>';
    return;
  }

  container.innerHTML = servers.map((s, i) =>
    '<div class="server-item' + (s.enabled === false ? ' disabled' : '') + '" data-idx="' + i + '">' +
      '<div class="server-header">' +
        '<span class="server-name">' + escHtml(s.name || 'Server ' + (i+1)) + '</span>' +
        '<span class="server-actions">' +
          '<input type="checkbox" class="server-toggle" ' + (s.enabled !== false ? 'checked' : '') +
          ' onchange="toggleServer(' + i + ', this.checked)" title="Enable/disable" />' +
          '<button class="btn-remove-server" onclick="removeServer(' + i + ')">Remove</button>' +
        '</span>' +
      '</div>' +
      '<div class="server-fields">' +
        '<div class="full"><label>Base URL</label>' +
          '<input type="text" value="' + escAttr(s.base_url || '') + '" ' +
          'placeholder="https://openrouter.ai/api/v1" onchange="updateServer(' + i + ',\'base_url\',this.value)" /></div>' +
        '<div class="full"><label>API Key</label>' +
          '<input type="password" value="' + escAttr(s.api_key || '') + '" ' +
          'placeholder="sk-..." onchange="updateServer(' + i + ',\'api_key\',this.value)" /></div>' +
        '<div><label>Model</label>' +
          '<input type="text" value="' + escAttr(s.model || '') + '" ' +
          'placeholder="google/gemma-3-27b-it" onchange="updateServer(' + i + ',\'model\',this.value)" /></div>' +
        '<div><label>Concurrency</label>' +
          '<input type="number" value="' + (s.concurrency || 4) + '" min="1" max="50" ' +
          'onchange="updateServer(' + i + ',\'concurrency\',parseInt(this.value)||4)" /></div>' +
      '</div>' +
    '</div>'
  ).join('');
}

function addServer() {
  const servers = getServers();
  servers.push({
    name: 'Server ' + (servers.length + 1),
    base_url: '',
    api_key: '',
    model: '',
    concurrency: 4,
    enabled: true,
  });
  saveServers(servers);
  renderServers();
}

function removeServer(idx) {
  const servers = getServers();
  servers.splice(idx, 1);
  saveServers(servers);
  renderServers();
}

function updateServer(idx, field, value) {
  const servers = getServers();
  if (servers[idx]) {
    servers[idx][field] = value;
    // Auto-update name from URL if name is default
    if (field === 'base_url' && servers[idx].name.startsWith('Server ')) {
      try {
        const host = new URL(value).hostname;
        if (host) servers[idx].name = host;
      } catch {}
    }
    saveServers(servers);
    if (field === 'base_url') renderServers();
  }
}

function toggleServer(idx, enabled) {
  const servers = getServers();
  if (servers[idx]) {
    servers[idx].enabled = enabled;
    saveServers(servers);
    renderServers();
  }
}

function escAttr(s) {
  return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* --- SMB --- */

function connectSmb() {
  const server = document.getElementById('smbServer').value.trim();
  const share = document.getElementById('smbShare').value.trim();
  const username = document.getElementById('smbUser').value.trim();
  const password = document.getElementById('smbPass').value;
  const domain = document.getElementById('smbDomain').value.trim();

  if (!server || !share) {
    document.getElementById('smbError').textContent = 'Server and share are required';
    return;
  }

  document.getElementById('smbError').textContent = '';
  document.getElementById('smbStatus').textContent = 'Connecting...';

  fetch('/smb/connect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({server, share, username, password, domain})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      document.getElementById('smbError').textContent = data.error;
      document.getElementById('smbStatus').textContent = '';
    } else {
      document.getElementById('smbStatus').textContent =
        'Connected! Mounted at ' + data.path;
      document.getElementById('smbPass').value = '';
      refreshMounts();
      // Auto-fill the folder path
      document.getElementById('folderPath').value = data.path;
    }
  })
  .catch(e => {
    document.getElementById('smbError').textContent = 'Connection failed: ' + e;
    document.getElementById('smbStatus').textContent = '';
  });
}

function refreshMounts() {
  fetch('/smb/list')
  .then(r => r.json())
  .then(data => {
    const container = document.getElementById('smbMounts');
    if (!data.mounts.length) {
      container.innerHTML = '';
      return;
    }
    container.innerHTML = data.mounts.map(m =>
      '<div class="smb-mount-item">' +
      '<span><span class="mount-name">' + m.name + '</span> ' +
      '<span class="mount-path">' + m.path + '</span></span>' +
      '<button class="btn-disconnect" onclick="disconnectSmb(\'' + m.name + '\')">Disconnect</button>' +
      '</div>'
    ).join('');
  });
}

function disconnectSmb(name) {
  fetch('/smb/disconnect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      document.getElementById('smbError').textContent = data.error;
    } else {
      document.getElementById('smbStatus').textContent = 'Disconnected';
      refreshMounts();
    }
  });
}

/* --- File Browser --- */

function openBrowser() {
  const modal = document.getElementById('browserModal');
  modal.classList.add('visible');
  // Start browsing from the current folder path or /mnt/smb or /
  let startPath = document.getElementById('folderPath').value.trim() || '/mnt/smb';
  browseTo(startPath);
}

function closeBrowser() {
  document.getElementById('browserModal').classList.remove('visible');
}

function browseTo(path) {
  currentBrowsePath = path;
  const body = document.getElementById('browserBody');
  body.innerHTML = '<div class="browse-loading">Loading...</div>';

  fetch('/browse?path=' + encodeURIComponent(path))
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      body.innerHTML = '<div class="browse-empty">' + data.error + '</div>';
      return;
    }

    currentBrowsePath = data.path;
    updateBreadcrumb(data.path);

    if (!data.entries.length) {
      body.innerHTML = '<div class="browse-empty">No video files or folders found</div>';
      return;
    }

    let html = '<ul class="file-list">';

    // Parent directory link
    if (data.parent) {
      html += '<li class="file-item dir" onclick="browseTo(\'' +
        escHtml(data.parent) + '\')">' +
        '<span class="file-icon">&#x1F4C1;</span>' +
        '<span class="file-name">..</span>' +
        '</li>';
    }

    data.entries.forEach(e => {
      if (e.type === 'dir') {
        html += '<li class="file-item dir" onclick="browseTo(\'' +
          escHtml(e.path) + '\')">' +
          '<span class="file-icon">&#x1F4C1;</span>' +
          '<span class="file-name">' + escHtml(e.name) + '</span>' +
          '</li>';
      } else {
        html += '<li class="file-item file" onclick="selectFile(\'' +
          escHtml(e.path) + '\')">' +
          '<span class="file-icon">&#x1F3AC;</span>' +
          '<span class="file-name">' + escHtml(e.name) + '</span>' +
          '<span class="file-size">' + e.size + '</span>' +
          '</li>';
      }
    });

    html += '</ul>';
    body.innerHTML = html;
  })
  .catch(e => {
    body.innerHTML = '<div class="browse-empty">Error: ' + e + '</div>';
  });
}

function updateBreadcrumb(path) {
  const bc = document.getElementById('browserBreadcrumb');
  const parts = path.split('/').filter(Boolean);
  let html = '<span class="crumb" onclick="browseTo(\'/\')">/</span>';
  let cumulative = '';
  parts.forEach((part, i) => {
    cumulative += '/' + part;
    const p = cumulative;
    html += '<span class="sep">/</span>';
    html += '<span class="crumb" onclick="browseTo(\'' + escHtml(p) + '\')">' +
      escHtml(part) + '</span>';
  });
  bc.innerHTML = html;
}

function selectFile(path) {
  document.getElementById('folderPath').value = path;
  closeBrowser();
}

function selectBrowsed() {
  document.getElementById('folderPath').value = currentBrowsePath;
  closeBrowser();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/'/g,"\\'").replace(/"/g,'&quot;');
}

/* --- Scan & Process --- */

function scanFolder() {
  const folder = document.getElementById('folderPath').value.trim();
  if (!folder) return;
  scannedFolder = folder;

  fetch('/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({folder})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      alert(data.error);
      return;
    }

    const info = document.getElementById('scanInfo');
    const summary = document.getElementById('scanSummary');
    const list = document.getElementById('streamList');
    const langSelect = document.getElementById('optLanguage');

    summary.textContent =
      data.videos + ' video(s), ' + data.streams + ' subtitle stream(s)';

    list.innerHTML = data.stream_info.map(s =>
      '<div>' + s.file +
      ' <span class="codec">[' + s.codec + ']</span>' +
      ' <span class="lang">' + s.language + '</span>' +
      (s.forced ? ' <span class="forced">forced</span>' : '') +
      '</div>'
    ).join('');

    langSelect.innerHTML = '';
    data.languages.forEach(l => {
      langSelect.innerHTML += '<option value="' + l + '" selected>' + l + '</option>';
    });

    info.classList.add('visible');
    document.getElementById('btnStart').disabled = data.streams === 0;
    document.getElementById('btnEstimate').disabled = data.streams === 0;
  });
}

function runEstimate() {
  const folder = scannedFolder || document.getElementById('folderPath').value.trim();
  if (!folder) return;

  const btn = document.getElementById('btnEstimate');
  const result = document.getElementById('estimateResult');
  btn.disabled = true;
  btn.textContent = 'Benchmarking...';
  result.classList.remove('visible');

  const subsPerFile = parseInt(document.getElementById('estSubsPerFile').value) || 400;
  const langSelect = document.getElementById('optLanguage');
  const language = Array.from(langSelect.selectedOptions).map(o => o.value);
  const forcedOnly = document.getElementById('optForced').checked;
  const enabledServers = getEnabledServers();

  fetch('/estimate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      folder, subs_per_stream: subsPerFile,
      servers: enabledServers.length ? enabledServers : undefined,
      language: language.length ? language : null,
      forced_only: forcedOnly
    })
  })
  .then(r => r.json())
  .then(data => {
    btn.disabled = false;
    btn.textContent = 'Estimate';
    if (data.error) {
      document.getElementById('estTime').textContent = 'Error: ' + data.error;
      document.getElementById('estDetails').textContent = '';
      result.classList.add('visible');
      return;
    }
    document.getElementById('estTime').textContent = data.total_time_estimate;
    document.getElementById('estDetails').innerHTML =
      'Avg per subtitle: ' + data.avg_seconds_per_sub + 's ' +
      '(sampled ' + data.sample_count + ' frames)<br>' +
      'First stream actual frames: ' + data.first_stream_actual_frames + '<br>' +
      'Estimate: ' + data.total_streams + ' streams x ' + data.subs_per_stream +
      ' subs = ' + data.total_subs_estimate + ' total<br>' +
      'Servers: ' + data.num_servers + ', Total concurrency: ' + data.concurrency + '<br>' +
      'Sample times: ' + data.sample_times.join('s, ') + 's';
    result.classList.add('visible');
  })
  .catch(e => {
    btn.disabled = false;
    btn.textContent = 'Estimate';
    document.getElementById('estTime').textContent = 'Error: ' + e;
    result.classList.add('visible');
  });
}

function startProcessing() {
  const folder = scannedFolder || document.getElementById('folderPath').value.trim();
  if (!folder) return;

  document.getElementById('btnStart').disabled = true;
  document.getElementById('btnStop').style.display = '';
  document.getElementById('progressArea').classList.add('visible');
  document.getElementById('log').innerHTML = '';

  let totalStreams = 0;
  let doneStreams = 0;

  function updateOverall() {
    document.getElementById('opCount').textContent = doneStreams + '/' + totalStreams + ' streams';
    const pct = totalStreams > 0 ? (doneStreams / totalStreams * 100).toFixed(1) : 0;
    document.getElementById('opFill').style.width = pct + '%';
  }

  if (eventSource) eventSource.close();
  eventSource = new EventSource('/events');

  eventSource.addEventListener('status', e => {
    addLog(JSON.parse(e.data).message, 'status');
  });

  eventSource.addEventListener('streams', e => {
    const d = JSON.parse(e.data);
    totalStreams = d.streams.length;
    updateOverall();
    d.streams.forEach(s => addLog('  ' + s, 'status'));
  });

  eventSource.addEventListener('stream_start', e => {
    const d = JSON.parse(e.data);
    addLog('\n[' + (d.stream_idx+1) + '/' + d.total_streams + '] ' + d.label, 'phase');
  });

  eventSource.addEventListener('stream_phase', e => {
    const d = JSON.parse(e.data);
    addLog('  ' + d.phase, 'status');
  });

  eventSource.addEventListener('stream_ocr_start', e => {
    const d = JSON.parse(e.data);
    const cp = document.getElementById('currentProgress');
    cp.classList.add('visible');
    document.getElementById('cpLabel').textContent = 'OCR in progress...';
    document.getElementById('cpCount').textContent = '0/' + d.total_frames;
    document.getElementById('cpFill').style.width = '0%';
    document.getElementById('cpText').textContent = '';
  });

  eventSource.addEventListener('stream_ocr_progress', e => {
    const d = JSON.parse(e.data);
    const pct = (d.completed / d.total * 100).toFixed(1);
    document.getElementById('cpCount').textContent = d.completed + '/' + d.total;
    document.getElementById('cpFill').style.width = pct + '%';
    document.getElementById('cpText').textContent = d.text;
  });

  eventSource.addEventListener('eta', e => {
    const d = JSON.parse(e.data);
    const eta = document.getElementById('etaDisplay');
    eta.classList.add('visible');
    document.getElementById('etaRemaining').textContent = d.remaining + ' remaining';
    document.getElementById('etaFinish').textContent = 'until ' + d.finish;
    document.getElementById('etaDetail').textContent = d.done + '/' + d.total + ' subs, ' + d.avg + 's/sub';
  });

  eventSource.addEventListener('stream_done', e => {
    const d = JSON.parse(e.data);
    let msg = '  Done -> ' + d.output + ' (' + d.total_frames + ' frames)';
    if (d.errors > 0) msg += ' [' + d.errors + ' errors]';
    addLog(msg, 'done');
    doneStreams++;
    updateOverall();
    document.getElementById('currentProgress').classList.remove('visible');
  });

  eventSource.addEventListener('stream_error', e => {
    const d = JSON.parse(e.data);
    addLog('  ' + d.error, 'error');
    doneStreams++;
    updateOverall();
    document.getElementById('currentProgress').classList.remove('visible');
  });

  eventSource.addEventListener('stream_skip', e => {
    const d = JSON.parse(e.data);
    addLog('  Skip: ' + d.label + ' (' + d.reason + ')', 'skip');
    doneStreams++;
    updateOverall();
  });

  eventSource.addEventListener('done', e => {
    const d = JSON.parse(e.data);
    addLog('\n' + d.message, 'done');
    document.getElementById('btnStart').disabled = false;
    document.getElementById('btnStop').style.display = 'none';
    document.getElementById('currentProgress').classList.remove('visible');
    document.getElementById('etaDisplay').classList.remove('visible');
    eventSource.close();
  });

  const enabledServers = getEnabledServers();

  fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      folder: folder,
      language: Array.from(document.getElementById('optLanguage').selectedOptions).map(o => o.value),
      forced_only: document.getElementById('optForced').checked,
      output_format: document.getElementById('optFormat').value,
      servers: enabledServers.length ? enabledServers : undefined,
    })
  });
}

function stopProcessing() {
  fetch('/stop', {method: 'POST'}).then(() => {
    document.getElementById('btnStart').disabled = false;
    document.getElementById('btnStop').style.display = 'none';
    document.getElementById('currentProgress').classList.remove('visible');
  });
}

function clearLog() {
  document.getElementById('log').innerHTML = '';
  document.getElementById('currentProgress').classList.remove('visible');
}

function addLog(text, cls) {
  const log = document.getElementById('log');
  const div = document.createElement('div');
  div.className = 'log-entry ' + cls;
  div.textContent = text;
  log.prepend(div);
}

// Enter key on folder input
document.getElementById('folderPath').addEventListener('keydown', e => {
  if (e.key === 'Enter') scanFolder();
});

// Load mounts and servers on startup
refreshMounts();
renderServers();

// Check for running job on page load
fetch('/status').then(r => r.json()).then(data => {
  if (!data.running) return;
  const p = data.progress;
  if (!p) return;

  // Show progress area with current state
  document.getElementById('btnStart').disabled = true;
  document.getElementById('btnStop').style.display = '';
  document.getElementById('progressArea').classList.add('visible');

  // Restore log
  const log = document.getElementById('log');
  p.log.forEach(msg => {
    const div = document.createElement('div');
    div.className = 'log-entry status';
    div.textContent = msg;
    log.prepend(div);
  });

  // Restore overall progress
  if (p.total_streams > 0) {
    document.getElementById('opCount').textContent =
      p.done_streams + '/' + p.total_streams + ' streams';
    const pct = (p.done_streams / p.total_streams * 100).toFixed(1);
    document.getElementById('opFill').style.width = pct + '%';
  }

  // Restore ETA
  if (p.eta_seconds > 0 && p.eta_finish) {
    const eta = document.getElementById('etaDisplay');
    eta.classList.add('visible');
    const h = Math.floor(p.eta_seconds / 3600);
    const m = Math.floor((p.eta_seconds % 3600) / 60);
    const dur = h > 0 ? h + 'h ' + m + 'm' : m + 'm';
    document.getElementById('etaRemaining').textContent = dur + ' remaining';
    document.getElementById('etaFinish').textContent = 'until ' + p.eta_finish;
    document.getElementById('etaDetail').textContent =
      p.total_subs_done + '/' + p.total_subs_estimate + ' subs';
  }

  // Restore current stream progress
  if (p.current_total > 0 && p.current_completed < p.current_total) {
    const cp = document.getElementById('currentProgress');
    cp.classList.add('visible');
    document.getElementById('cpLabel').textContent = p.current_label || 'OCR in progress...';
    document.getElementById('cpCount').textContent =
      p.current_completed + '/' + p.current_total;
    const pct = (p.current_completed / p.current_total * 100).toFixed(1);
    document.getElementById('cpFill').style.width = pct + '%';
    document.getElementById('cpText').textContent = p.current_text || '';
  }

  // Connect to SSE for live updates from here on
  let totalStreams = p.total_streams;
  let doneStreams = p.done_streams;

  function updateOverall() {
    document.getElementById('opCount').textContent = doneStreams + '/' + totalStreams + ' streams';
    const pct = totalStreams > 0 ? (doneStreams / totalStreams * 100).toFixed(1) : 0;
    document.getElementById('opFill').style.width = pct + '%';
  }

  if (eventSource) eventSource.close();
  eventSource = new EventSource('/events');

  eventSource.addEventListener('stream_ocr_progress', e => {
    const d = JSON.parse(e.data);
    const pct = (d.completed / d.total * 100).toFixed(1);
    document.getElementById('cpCount').textContent = d.completed + '/' + d.total;
    document.getElementById('cpFill').style.width = pct + '%';
    document.getElementById('cpText').textContent = d.text;
  });

  eventSource.addEventListener('eta', e => {
    const d = JSON.parse(e.data);
    const eta = document.getElementById('etaDisplay');
    eta.classList.add('visible');
    document.getElementById('etaRemaining').textContent = d.remaining + ' remaining';
    document.getElementById('etaFinish').textContent = 'until ' + d.finish;
    document.getElementById('etaDetail').textContent = d.done + '/' + d.total + ' subs, ' + d.avg + 's/sub';
  });

  eventSource.addEventListener('stream_ocr_start', e => {
    const d = JSON.parse(e.data);
    const cp = document.getElementById('currentProgress');
    cp.classList.add('visible');
    document.getElementById('cpLabel').textContent = 'OCR in progress...';
    document.getElementById('cpCount').textContent = '0/' + d.total_frames;
    document.getElementById('cpFill').style.width = '0%';
    document.getElementById('cpText').textContent = '';
  });

  eventSource.addEventListener('stream_start', e => {
    const d = JSON.parse(e.data);
    addLog('[' + (d.stream_idx+1) + '/' + d.total_streams + '] ' + d.label, 'phase');
  });

  eventSource.addEventListener('stream_done', e => {
    const d = JSON.parse(e.data);
    let msg = '  Done -> ' + d.output + ' (' + d.total_frames + ' frames)';
    if (d.errors > 0) msg += ' [' + d.errors + ' errors]';
    addLog(msg, 'done');
    doneStreams++;
    updateOverall();
    document.getElementById('currentProgress').classList.remove('visible');
  });

  eventSource.addEventListener('stream_error', e => {
    const d = JSON.parse(e.data);
    addLog('  ' + d.error, 'error');
    doneStreams++;
    updateOverall();
    document.getElementById('currentProgress').classList.remove('visible');
  });

  eventSource.addEventListener('stream_skip', e => {
    const d = JSON.parse(e.data);
    addLog('  Skip: ' + d.label + ' (' + d.reason + ')', 'skip');
    doneStreams++;
    updateOverall();
  });

  eventSource.addEventListener('status', e => {
    addLog(JSON.parse(e.data).message, 'status');
  });

  eventSource.addEventListener('done', e => {
    const d = JSON.parse(e.data);
    addLog('\n' + d.message, 'done');
    document.getElementById('btnStart').disabled = false;
    document.getElementById('btnStop').style.display = 'none';
    document.getElementById('currentProgress').classList.remove('visible');
    document.getElementById('etaDisplay').classList.remove('visible');
    eventSource.close();
  });
});
</script>
</body>
</html>
"""


def main():
    port = 8642
    # Bind to 0.0.0.0 in Docker, 127.0.0.1 otherwise
    bind = "0.0.0.0" if os.environ.get("SUBTITLER_DOCKER") else "127.0.0.1"

    # Auto-mount SMB share from env vars if configured
    _auto_mount_smb()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer((bind, port), GUIHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Subtitler GUI running at {url}")
    if not os.environ.get("SUBTITLER_DOCKER"):
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
