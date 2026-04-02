from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryRecord:
    id: str
    type: str
    status: str
    confidence: str
    subject: str
    summary: str
    tags: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    scope_reason: str = ""
    pin_until: str | None = None
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    rationale: str | None = None
    next_use: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "confidence": self.confidence,
            "subject": self.subject,
            "summary": self.summary,
            "tags": list(self.tags),
            "source_refs": list(self.source_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scope_reason": self.scope_reason,
            "pin_until": self.pin_until,
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "rationale": self.rationale,
            "next_use": self.next_use,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryRecord":
        return cls(
            id=str(payload["id"]),
            type=str(payload["type"]),
            status=str(payload["status"]),
            confidence=str(payload.get("confidence", "medium")),
            subject=str(payload["subject"]),
            summary=str(payload["summary"]),
            tags=[str(item) for item in payload.get("tags", [])],
            source_refs=[str(item) for item in payload.get("source_refs", [])],
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            scope_reason=str(payload.get("scope_reason", "")),
            pin_until=payload.get("pin_until"),
            supersedes=[str(item) for item in payload.get("supersedes", [])],
            superseded_by=payload.get("superseded_by"),
            rationale=payload.get("rationale"),
            next_use=payload.get("next_use"),
        )


@dataclass(slots=True)
class MemoryDocument:
    scope: str
    metadata: dict[str, Any]
    sections: dict[str, list[MemoryRecord]]

    @property
    def revision(self) -> int:
        return int(self.metadata.get("revision", 0))


@dataclass(slots=True)
class Snapshot:
    revision: str
    global_records: list[MemoryRecord]
    local_records: list[MemoryRecord]
    rendered_text: str
    source_fingerprint: str
    built_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "global_records": [record.to_dict() for record in self.global_records],
            "local_records": [record.to_dict() for record in self.local_records],
            "rendered_text": self.rendered_text,
            "source_fingerprint": self.source_fingerprint,
            "built_at": self.built_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Snapshot":
        return cls(
            revision=str(payload["revision"]),
            global_records=[
                MemoryRecord.from_dict(record) for record in payload.get("global_records", [])
            ],
            local_records=[
                MemoryRecord.from_dict(record) for record in payload.get("local_records", [])
            ],
            rendered_text=str(payload["rendered_text"]),
            source_fingerprint=str(payload["source_fingerprint"]),
            built_at=str(payload["built_at"]),
        )
