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
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .assembler import build_output_path, write_subtitles
from .config import Config, load_config
from .extractor import extract_stream
from .ocr import OCRClient
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


def _broadcast(event_type: str, data: dict):
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    for q in _event_queues:
        q.put(msg)


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


def _run_pipeline(config: Config):
    global _is_running, _stop_requested
    _is_running = True
    _stop_requested = False

    try:
        root = Path(config.input_path)
        videos = scan_videos(root)
        _broadcast("status", {"message": f"Found {len(videos)} video file(s)"})

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
        ocr_client = OCRClient(config)
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

                # Render all streams for this video in parallel
                prepared = {}
                with ThreadPoolExecutor(max_workers=min(len(work_items), 6)) as pool:
                    futures = {}
                    for si, stream, label, out_path, tmp_dir in work_items:
                        _broadcast("stream_start", {
                            "stream_idx": si, "label": label,
                            "total_streams": len(all_streams),
                        })
                        _broadcast("stream_phase", {"stream_idx": si, "phase": "Rendering..."})
                        fut = pool.submit(_prepare_one_stream, stream, tmp_dir, label, si)
                        futures[fut] = (si, stream, label, out_path, tmp_dir)

                    for fut in as_completed(futures):
                        si, stream, label, out_path, tmp_dir = futures[fut]
                        frames, error = fut.result()

                        if error:
                            _broadcast("stream_error", {"stream_idx": si, "error": error})
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        elif not frames:
                            _broadcast("stream_error", {"stream_idx": si, "error": "No frames found"})
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        else:
                            _broadcast("stream_phase", {
                                "stream_idx": si,
                                "phase": f"{len(frames)} frames ready",
                            })
                            prepared[si] = (frames, out_path, stream, tmp_dir)

                if not prepared:
                    continue

                # OCR all streams for this video
                for si in sorted(prepared.keys()):
                    _check_stop()
                    frames, out_path, stream, tmp_dir = prepared[si]
                    codec = "PGS" if stream.is_pgs else "VobSub"
                    label = f"{stream.source_file.stem} [{codec} {stream.lang_code}]"

                    _broadcast("stream_ocr_start", {
                        "stream_idx": si,
                        "total_frames": len(frames),
                    })

                    completed = 0

                    def on_progress(idx, result, _si=si, _total=len(frames)):
                        nonlocal completed
                        completed += 1
                        _broadcast("stream_ocr_progress", {
                            "stream_idx": _si,
                            "completed": completed,
                            "total": _total,
                            "text": truncate(result.text, 80) if result.text else "",
                        })

                    results = loop.run_until_complete(
                        ocr_client.ocr_frames(frames, stream.language, on_progress)
                    )

                    write_subtitles(results, out_path, config.output_format)
                    errors = sum(1 for r in results if r.text.startswith("[OCR ERROR"))
                    _broadcast("stream_done", {
                        "stream_idx": si,
                        "output": out_path.name,
                        "total_frames": len(frames),
                        "errors": errors,
                    })

                    shutil.rmtree(tmp_dir, ignore_errors=True)

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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"running": _is_running}).encode())

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

            config = load_config(cli_overrides={
                "input_path": unquote(body.get("folder", ".")).strip(),
                "language": body.get("language") or None,
                "forced_only": body.get("forced_only", False),
                "output_format": body.get("output_format", "vtt"),
                "model": body.get("model") or None,
                "concurrency": body.get("concurrency") or None,
            })

            thread = threading.Thread(target=_run_pipeline, args=(config,), daemon=True)
            thread.start()

            self._json_response({"status": "started"})

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
    <div class="option">
      <label>Model</label>
      <input type="text" id="optModel" placeholder="from config" />
    </div>
    <div class="option">
      <label>Concurrency</label>
      <input type="number" id="optConcurrency" placeholder="10" min="1" max="50" />
    </div>
    <div class="option checkbox-option" style="grid-column: span 2;">
      <input type="checkbox" id="optForced" />
      <label style="margin: 0; text-transform: none; font-size: 0.95rem; color: #ccc;">
        Forced subtitles only
      </label>
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
      html += '<li class="file-item dir" ondblclick="browseTo(\'' +
        escHtml(data.parent) + '\')">' +
        '<span class="file-icon">&#x1F4C1;</span>' +
        '<span class="file-name">..</span>' +
        '</li>';
    }

    data.entries.forEach(e => {
      if (e.type === 'dir') {
        html += '<li class="file-item dir" ondblclick="browseTo(\'' +
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
    eventSource.close();
  });

  fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      folder: folder,
      language: Array.from(document.getElementById('optLanguage').selectedOptions).map(o => o.value),
      forced_only: document.getElementById('optForced').checked,
      output_format: document.getElementById('optFormat').value,
      model: document.getElementById('optModel').value || null,
      concurrency: parseInt(document.getElementById('optConcurrency').value) || null,
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

// Load mounts on startup
refreshMounts();
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

    server = HTTPServer((bind, port), GUIHandler)
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
