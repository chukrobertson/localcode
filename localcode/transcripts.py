from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from .database import Database
from .paths import transcript_dir


def export_chat(database: Database, project_id: str, chat_id: str, project_path: str) -> Path:
    destination = transcript_dir(project_id) / f"{chat_id}.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    records: list[str] = []
    for message in database.list_messages(chat_id):
        if message.role not in {"user", "assistant"}:
            continue
        message_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"localcode:{chat_id}:{message.id}"))
        record = {
            "type": message.role,
            "uuid": message_uuid,
            "sessionId": chat_id,
            "timestamp": message.created_at,
            "cwd": project_path,
            "message": {"role": message.role, "content": message.content},
        }
        records.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    payload = "\n".join(records) + ("\n" if records else "")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        delete=False,
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination
