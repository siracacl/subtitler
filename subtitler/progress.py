from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    MofNCompleteColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .models import OCRResult

console = Console()


def create_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("[dim]{task.fields[status]}"),
        console=console,
    )


def truncate(text: str, max_len: int = 60) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text
