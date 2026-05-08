from __future__ import annotations

import json
import logging
import pathlib
from typing import TYPE_CHECKING, Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .. import config
from .base import OutputWriter

if TYPE_CHECKING:
    from datetime import datetime

    from ..session import TranscriptEntry

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
_STATE_PATH = pathlib.Path("~/.resilient-finch/state.json").expanduser()
_DOC_TITLE = "Resilient Finch Transcripts"


def _load_state() -> dict[str, Any]:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2))


class GoogleDocsWriter(OutputWriter):
    def __init__(self) -> None:
        creds_path = pathlib.Path(config.GOOGLE_SERVICE_ACCOUNT_PATH).expanduser()
        if not creds_path.exists():
            msg = f"Service account key not found: {creds_path}"
            raise FileNotFoundError(msg)

        creds = service_account.Credentials.from_service_account_file(
            str(creds_path), scopes=_SCOPES
        )
        self._docs = build("docs", "v1", credentials=creds)
        self._drive = build("drive", "v3", credentials=creds)
        self._doc_id: str | None = None
        self._tab_id: str | None = None
        self._start_time: datetime | None = None

    def write_header(self, topic: str, start_time: datetime) -> None:
        self._start_time = start_time
        try:
            doc_id = self._resolve_doc_id()
            tab_id = self._create_tab(doc_id, topic or "Untitled")
            self._doc_id = doc_id
            self._tab_id = tab_id

            lines: list[str] = ["=" * 60]
            if topic:
                lines.append(f"  Topic:   {topic}")
            lines += [
                f"  Date:    {start_time.strftime('%Y-%m-%d')}",
                f"  Started: {start_time.strftime('%H:%M:%S')} UTC",
                "=" * 60,
                "",
            ]
            self._append_text("\n".join(lines) + "\n")
        except Exception:
            logger.exception("GoogleDocsWriter: failed to write header")

    def write_entry(self, entry: TranscriptEntry) -> None:
        if self._doc_id is None or self._tab_id is None:
            return
        try:
            self._append_text(entry.format_line())
        except Exception:
            logger.exception("GoogleDocsWriter: failed to write entry")

    def write_footer(self, end_time: datetime) -> None:
        if self._doc_id is None or self._tab_id is None:
            return
        try:
            start = self._start_time or end_time
            total_seconds = int((end_time - start).total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                duration_str = f"{hours}h {minutes}m {seconds}s"
            elif minutes:
                duration_str = f"{minutes}m {seconds}s"
            else:
                duration_str = f"{seconds}s"

            lines = [
                "",
                "=" * 60,
                f"  Ended:    {end_time.strftime('%H:%M:%S')} UTC",
                f"  Duration: {duration_str}",
                "=" * 60,
            ]
            self._append_text("\n".join(lines) + "\n")
        except Exception:
            logger.exception("GoogleDocsWriter: failed to write footer")

    def close(self) -> None:
        pass

    def _resolve_doc_id(self) -> str:
        if config.GOOGLE_DOCS_DOC_ID:
            return config.GOOGLE_DOCS_DOC_ID

        state = _load_state()
        if "doc_id" in state:
            return state["doc_id"]

        doc = self._docs.documents().create(body={"title": _DOC_TITLE}).execute()
        doc_id: str = doc["documentId"]

        state["doc_id"] = doc_id
        _save_state(state)

        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        logger.info("Created new Google Doc: %s", url)
        logger.info("To access this doc, share it with yourself from the service account.")

        return doc_id

    def _create_tab(self, doc_id: str, title: str) -> str:
        result = (
            self._docs.documents()
            .batchUpdate(
                documentId=doc_id,
                body={"requests": [{"createTab": {"tabProperties": {"title": title}}}]},
            )
            .execute()
        )
        tab_props: dict[str, Any] = result["replies"][0]["createTab"]["tab"]["tabProperties"]
        return tab_props["tabId"]

    def _append_text(self, text: str) -> None:
        self._docs.documents().batchUpdate(
            documentId=self._doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "endOfSegmentLocation": {"tabId": self._tab_id},
                            "text": text,
                        }
                    }
                ]
            },
        ).execute()
