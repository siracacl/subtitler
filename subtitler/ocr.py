import asyncio
import base64
import json
import time

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


class OCRClient:
    def __init__(self, config: Config, stop_check=None):
        self.config = config
        self.stop_check = stop_check  # callable that returns True if stop requested
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.client = httpx.AsyncClient(
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

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
            prompt = self.config.prompt.format(language=lang_name)

            payload = {
                "model": self.config.model,
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

            url = f"{self.config.base_url.rstrip('/')}/chat/completions"

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
