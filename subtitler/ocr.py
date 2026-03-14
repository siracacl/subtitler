import asyncio
import base64
import json
import time
from dataclasses import dataclass

import httpx

from .config import Config
from .models import SubtitleFrame, OCRResult


# ISO 639-2/B to full language name mapping
LANG_NAMES = {
    "eng": "English", "fre": "French", "fra": "French",
    "ger": "German", "deu": "German", "spa": "Spanish",
    "ita": "Italian", "por": "Portuguese", "dut": "Dutch",
    "nld": "Dutch", "rus": "Russian", "jpn": "Japanese",
    "chi": "Chinese", "zho": "Chinese", "kor": "Korean",
    "ara": "Arabic", "hin": "Hindi", "tur": "Turkish",
    "pol": "Polish", "swe": "Swedish", "nor": "Norwegian",
    "dan": "Danish", "fin": "Finnish", "cze": "Czech",
    "ces": "Czech", "hun": "Hungarian", "rum": "Romanian",
    "ron": "Romanian", "gre": "Greek", "ell": "Greek",
    "heb": "Hebrew", "tha": "Thai", "vie": "Vietnamese",
    "ind": "Indonesian", "may": "Malay", "msa": "Malay",
    "und": "Unknown",
}


def _language_name(code: str | None) -> str:
    if not code:
        return "Unknown"
    return LANG_NAMES.get(code, code)


@dataclass
class ServerConfig:
    """Configuration for a single API server."""
    name: str
    base_url: str
    api_key: str
    model: str
    concurrency: int = 4


class OCRClient:
    def __init__(self, config: Config = None, stop_check=None, server: ServerConfig = None):
        self.stop_check = stop_check
        if server:
            self.base_url = server.base_url
            self.model = server.model
            self.prompt = config.prompt if config else "Read the subtitle text in this image. The language is {language}. Return ONLY the subtitle text, nothing else. Preserve line breaks exactly as shown."
            self.semaphore = asyncio.Semaphore(server.concurrency)
            self.server_name = server.name
            self.client = httpx.AsyncClient(
                timeout=60.0,
                headers={
                    "Authorization": f"Bearer {server.api_key}",
                    "Content-Type": "application/json",
                },
            )
        elif config:
            self.base_url = config.base_url
            self.model = config.model
            self.prompt = config.prompt
            self.semaphore = asyncio.Semaphore(config.concurrency)
            self.server_name = "default"
            self.client = httpx.AsyncClient(
                timeout=60.0,
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
            )
        else:
            raise ValueError("Either config or server must be provided")

    async def close(self):
        await self.client.aclose()

    async def ocr_frame(
        self,
        frame: SubtitleFrame,
        language: str | None,
    ) -> OCRResult:
        async with self.semaphore:
            if self.stop_check and self.stop_check():
                return OCRResult(frame=frame, text="[STOPPED]")
            img_b64 = base64.b64encode(frame.image_bytes).decode("ascii")
            lang_name = _language_name(language)
            prompt = self.prompt.format(language=lang_name)

            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}",
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 1000,
                "chat_template_kwargs": {"enable_thinking": False},
            }

            url = f"{self.base_url.rstrip('/')}/chat/completions"

            for attempt in range(3):
                try:
                    resp = await self.client.post(url, json=payload)
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("retry-after", "2"))
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    text = content.strip() if content else ""
                    return OCRResult(frame=frame, text=text)
                except (httpx.HTTPStatusError, httpx.ReadTimeout, json.JSONDecodeError, KeyError, IndexError) as e:
                    if attempt == 2:
                        return OCRResult(frame=frame, text=f"[OCR ERROR: {e}]")
                    await asyncio.sleep(2 ** attempt)

            return OCRResult(frame=frame, text="[OCR ERROR: max retries]")

    async def ocr_frames(
        self,
        frames: list[SubtitleFrame],
        language: str | None,
        on_progress=None,
    ) -> list[OCRResult]:
        results: list[OCRResult | None] = [None] * len(frames)

        async def process(idx: int, frame: SubtitleFrame):
            result = await self.ocr_frame(frame, language)
            results[idx] = result
            if on_progress:
                on_progress(idx, result)

        tasks = [process(i, f) for i, f in enumerate(frames)]
        await asyncio.gather(*tasks)

        return [r for r in results if r is not None]


class MultiOCRClient:
    """Distributes OCR work across multiple API servers."""

    def __init__(self, config: Config, servers: list[ServerConfig], stop_check=None,
                 on_server_fail=None):
        self.clients: list[OCRClient] = []
        self.failed_servers: set[int] = set()
        self.on_server_fail = on_server_fail  # callback(server_name, error)
        self._lock: asyncio.Lock | None = None  # created lazily in async context
        for server in servers:
            client = OCRClient(config=config, stop_check=stop_check, server=server)
            self.clients.append(client)
        self.stop_check = stop_check

    async def close(self):
        for client in self.clients:
            await client.close()

    def _active_clients(self) -> list[tuple[int, OCRClient]]:
        return [(i, c) for i, c in enumerate(self.clients) if i not in self.failed_servers]

    async def _mark_failed(self, client_idx: int, error: str):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if client_idx not in self.failed_servers:
                self.failed_servers.add(client_idx)
                name = self.clients[client_idx].server_name
                if self.on_server_fail:
                    self.on_server_fail(name, error)

    async def ocr_frame(self, frame: SubtitleFrame, language: str | None) -> OCRResult:
        active = self._active_clients()
        if not active:
            return OCRResult(frame=frame, text="[OCR ERROR: all servers failed]")
        return await active[0][1].ocr_frame(frame, language)

    async def ocr_frames(
        self,
        frames: list[SubtitleFrame],
        language: str | None,
        on_progress=None,
    ) -> list[OCRResult]:
        """Distribute frames across all servers. Each server's semaphore limits its own concurrency.
        If a server becomes unreachable, its frames are retried on remaining servers."""
        results: list[OCRResult | None] = [None] * len(frames)

        async def process(idx: int, frame: SubtitleFrame):
            active = self._active_clients()
            if not active:
                result = OCRResult(frame=frame, text="[OCR ERROR: all servers failed]")
                results[idx] = result
                if on_progress:
                    on_progress(idx, result)
                return

            # Round-robin across active clients
            client_idx, client = active[idx % len(active)]
            result = await client.ocr_frame(frame, language)

            # Check if this was a connection error - retry on another server
            if result.text.startswith("[OCR ERROR:") and ("ConnectError" in result.text or
                    "ConnectTimeout" in result.text or "ConnectionRefused" in result.text):
                await self._mark_failed(client_idx, result.text)

                # Retry on remaining servers
                remaining = self._active_clients()
                if remaining:
                    fallback_idx, fallback = remaining[idx % len(remaining)]
                    result = await fallback.ocr_frame(frame, language)

            results[idx] = result
            if on_progress:
                on_progress(idx, result)

        tasks = [process(i, f) for i, f in enumerate(frames)]
        await asyncio.gather(*tasks)

        return [r for r in results if r is not None]
