from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULTS = {
    "api_key": "",
    "base_url": "https://openrouter.ai/api/v1",
    "model": "google/gemma-3-27b-it",
    "concurrency": 10,
    "output_format": "vtt",
    "prompt": (
        "Read the subtitle text in this image. "
        "The language is {language}. "
        "Return ONLY the subtitle text, nothing else. "
        "Preserve line breaks exactly as shown."
    ),
}

CONFIG_SEARCH_PATHS = [
    Path("subtitler.yaml"),
    Path("subtitler.yml"),
    Path.home() / ".config" / "subtitler" / "config.yaml",
]


@dataclass
class Config:
    api_key: str = ""
    base_url: str = DEFAULTS["base_url"]
    model: str = DEFAULTS["model"]
    concurrency: int = DEFAULTS["concurrency"]
    output_format: str = DEFAULTS["output_format"]
    prompt: str = DEFAULTS["prompt"]
    language: list[str] | None = None
    forced_only: bool = False
    dry_run: bool = False
    output_dir: str | None = None
    input_path: str = "."


def find_config_file() -> Path | None:
    for p in CONFIG_SEARCH_PATHS:
        if p.exists():
            return p
    return None


ENV_PREFIX = "SUBTITLER_"

# Maps env var names to (config key, type)
_ENV_MAP = {
    "SUBTITLER_API_KEY": ("api_key", str),
    "SUBTITLER_BASE_URL": ("base_url", str),
    "SUBTITLER_MODEL": ("model", str),
    "SUBTITLER_CONCURRENCY": ("concurrency", int),
    "SUBTITLER_OUTPUT_FORMAT": ("output_format", str),
    "SUBTITLER_PROMPT": ("prompt", str),
    "SUBTITLER_LANGUAGE": ("language", list),
    "SUBTITLER_FORCED_ONLY": ("forced_only", bool),
    "SUBTITLER_OUTPUT_DIR": ("output_dir", str),
}


def _load_env() -> dict:
    """Load config values from SUBTITLER_* environment variables."""
    import os
    result = {}
    for env_key, (config_key, typ) in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if typ is int:
            result[config_key] = int(val)
        elif typ is bool:
            result[config_key] = val.lower() in ("1", "true", "yes")
        elif typ is list:
            result[config_key] = [v.strip() for v in val.split(",") if v.strip()]
        else:
            result[config_key] = val
    return result


def load_config(
    config_path: str | None = None,
    cli_overrides: dict | None = None,
) -> Config:
    data: dict = {}

    # 1. Load from YAML file (lowest priority)
    path = Path(config_path) if config_path else find_config_file()
    if path and path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    # 2. Override with environment variables
    env_data = _load_env()
    for k, v in env_data.items():
        data[k] = v

    # 3. Override with CLI/web overrides (highest priority)
    if cli_overrides:
        for k, v in cli_overrides.items():
            if v is not None:
                data[k] = v

    return Config(**{k: v for k, v in data.items() if hasattr(Config, k)})
