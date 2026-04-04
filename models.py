from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Recording:
    id: str
    title: str
    date: datetime
    duration_minutes: int
    transcript: str
    has_summary: bool = False
    plaud_summary: str = ""


@dataclass
class ProcessedRecording:
    recording: Recording
    summary_md: str
    suggested_client: str
    rec_type: str = "reuniao"
    validated_client: str | None = None
    validated_type: str | None = None

    @property
    def client(self) -> str:
        return self.validated_client or self.suggested_client

    @property
    def filename(self) -> str:
        date_str = self.recording.date.strftime("%Y-%m-%d")
        safe_title = self.recording.title.replace("/", "-").replace("\\", "-")
        return f"{date_str} - {safe_title}.md"
