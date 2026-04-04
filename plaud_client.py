import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from models import Recording


class PlaudClient:
    def __init__(self, token: str, origin: str = "https://api.plaud.ai"):
        self.token = token
        self.origin = origin.rstrip("/")

    @property
    def _headers(self):
        return {
            "accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "edit-from": "web",
        }

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.origin}{path}"
        r = requests.request(method, url, headers=self._headers, timeout=30, **kwargs)
        data = r.json()

        # Region redirect
        if data.get("status") == -302:
            new_origin = data["data"]["domains"]["api"]
            if new_origin.startswith("http"):
                self.origin = new_origin.rstrip("/")
            else:
                self.origin = f"https://{new_origin}".rstrip("/")
            url = f"{self.origin}{path}"
            r = requests.request(method, url, headers=self._headers, timeout=30, **kwargs)
            data = r.json()

        if data.get("status", 0) != 0:
            raise Exception(f"Plaud API error: {data.get('msg', data.get('status'))}")

        return data.get("data")

    def _raw_request(self, method: str, path: str, **kwargs):
        """Retorna o JSON completo (não só o campo 'data')."""
        url = f"{self.origin}{path}"
        r = requests.request(method, url, headers=self._headers, timeout=30, **kwargs)
        data = r.json()

        if data.get("status") == -302:
            new_origin = data["data"]["domains"]["api"]
            if new_origin.startswith("http"):
                self.origin = new_origin.rstrip("/")
            else:
                self.origin = f"https://{new_origin}".rstrip("/")
            url = f"{self.origin}{path}"
            r = requests.request(method, url, headers=self._headers, timeout=30, **kwargs)
            data = r.json()

        if data.get("status", 0) != 0:
            raise Exception(f"Plaud API error: {data.get('msg', data.get('status'))}")

        return data

    def list_files(self, skip: int = 0, limit: int = 50) -> list[dict]:
        params = urlencode({
            "skip": skip,
            "limit": limit,
            "is_trash": 0,
            "sort_by": "start_time",
            "is_desc": "true",
        })
        data = self._raw_request("GET", f"/file/simple/web?{params}")
        return data.get("data_file_list", [])

    def list_all_files(self, since: datetime | None = None) -> list[dict]:
        """Lista todas as gravações, com paginação. Filtra por data se `since` fornecido."""
        all_files = []
        skip = 0
        limit = 50

        while True:
            batch = self.list_files(skip=skip, limit=limit)
            if not batch:
                break

            for f in batch:
                file_date = self._parse_start_time(f)
                if since and file_date and file_date < since:
                    return all_files
                all_files.append(f)

            if len(batch) < limit:
                break
            skip += limit

        return all_files

    def get_file_detail(self, file_id: str) -> dict:
        return self._request("GET", f"/file/detail/{file_id}")

    def get_transcript(self, file_id: str) -> str:
        """Extrai a transcrição completa de uma gravação."""
        detail = self.get_file_detail(file_id)

        # Tenta trans_result inline
        if detail.get("trans_result"):
            return self._format_transcript(detail["trans_result"])

        # Tenta content_list com link
        for item in detail.get("content_list", []):
            if item.get("data_type") == "transaction":
                if item.get("data_content"):
                    segments = json.loads(item["data_content"]) if isinstance(item["data_content"], str) else item["data_content"]
                    return self._format_transcript(segments)
                if item.get("data_link"):
                    return self._fetch_transcript_link(item["data_link"])

        # Tenta campos alternativos
        for field in ("transcript", "transcript_text"):
            if detail.get(field):
                return detail[field]

        return ""

    def get_summary(self, file_id: str) -> str:
        """Extrai o resumo do Plaud, se existir."""
        detail = self.get_file_detail(file_id)

        if detail.get("ai_content"):
            return detail["ai_content"]

        for item in detail.get("content_list", []):
            if item.get("data_type") != "transaction":
                if item.get("data_content"):
                    return item["data_content"]
                if item.get("data_link"):
                    return self._fetch_content_link(item["data_link"])

        return ""

    def get_recordings(self, since: datetime | None = None) -> list[Recording]:
        """Retorna gravações como objetos Recording com transcrição."""
        files = self.list_all_files(since=since)
        recordings = []

        for f in files:
            file_id = f.get("id", "")
            if not f.get("is_trans", False):
                continue  # Pula gravações sem transcrição

            transcript = self.get_transcript(file_id)
            if not transcript:
                continue

            date = self._parse_start_time(f) or datetime.now(timezone.utc)
            duration_ms = f.get("duration", 0)
            duration_min = duration_ms // 60_000 if duration_ms >= 60_000 else max(duration_ms // 1000 // 60, 1)

            recordings.append(Recording(
                id=file_id,
                title=f.get("filename", f.get("title", "Sem título")),
                date=date,
                duration_minutes=max(duration_min, 1),
                transcript=transcript,
                has_summary=f.get("is_summary", False),
            ))

        return recordings

    def _parse_start_time(self, file_data: dict) -> datetime | None:
        ts = file_data.get("start_time")
        if not ts:
            return None
        ts = int(ts)
        if ts >= 1_000_000_000_000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _format_transcript(self, segments: list[dict]) -> str:
        lines = []
        for seg in segments:
            speaker = seg.get("speaker", "")
            content = seg.get("content", "")
            if speaker:
                lines.append(f"[{speaker}]: {content}")
            else:
                lines.append(content)
        return "\n".join(lines)

    def _fetch_transcript_link(self, url: str) -> str:
        r = requests.get(url, timeout=30)
        content = r.content

        # Check for gzip
        if content[:2] == b'\x1f\x8b':
            content = gzip.decompress(content)

        try:
            data = json.loads(content)
            if isinstance(data, list):
                return self._format_transcript(data)
            if isinstance(data, dict) and "trans_result" in data:
                return self._format_transcript(data["trans_result"])
            return str(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return content.decode("utf-8", errors="replace")

    def _fetch_content_link(self, url: str) -> str:
        r = requests.get(url, timeout=30)
        content = r.content
        if content[:2] == b'\x1f\x8b':
            content = gzip.decompress(content)
        return content.decode("utf-8", errors="replace")
