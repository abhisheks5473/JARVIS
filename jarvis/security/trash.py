"""Deletion that can be taken back.

Guide section 14: move to a trash folder the agent controls, and let a cron
job empty it after 30 days. The reasoning is not that the model is untrusted
in the abstract -- it is that a 2% error rate is delightful for "summarise
this page" and catastrophic for "delete the old drafts".

Nothing in JARVIS calls os.remove on a user file. `delete_path` moves; only
`empty_expired`, running on a schedule with a date check, actually removes.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import config

MANIFEST = "manifest.json"


@dataclass
class TrashEntry:
    entry_id: str
    original_path: str
    deleted_at: float
    deleted_at_iso: str
    size_bytes: int
    kind: str

    @property
    def age_days(self) -> float:
        return (time.time() - self.deleted_at) / 86400.0

    @property
    def expires_in_days(self) -> float:
        return max(0.0, config.TRASH_RETENTION_DAYS - self.age_days)


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def _slug(name: str) -> str:
    keep = "-_. "
    cleaned = "".join(c if c.isalnum() or c in keep else "_" for c in name)
    return cleaned.strip().replace(" ", "_")[:60] or "item"


def send_to_trash(path: Path | str) -> TrashEntry:
    """Move `path` into the trash and return its receipt.

    Raises FileNotFoundError if there is nothing there -- callers report that
    as a tool error rather than pretending it succeeded.
    """
    source = Path(path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"nothing at {source}")

    now = time.time()
    entry_id = f"{int(now)}-{_slug(source.name)}"
    bucket = config.TRASH_DIR / entry_id
    bucket.mkdir(parents=True, exist_ok=True)

    size = _dir_size(source)
    kind = "dir" if source.is_dir() else "file"

    shutil.move(str(source), str(bucket / source.name))

    entry = TrashEntry(
        entry_id=entry_id,
        original_path=str(source),
        deleted_at=now,
        deleted_at_iso=datetime.now().astimezone().isoformat(timespec="seconds"),
        size_bytes=size,
        kind=kind,
    )
    (bucket / MANIFEST).write_text(
        json.dumps(entry.__dict__, indent=2), encoding="utf-8"
    )
    return entry


def list_trash() -> list[TrashEntry]:
    entries: list[TrashEntry] = []
    if not config.TRASH_DIR.exists():
        return entries
    for bucket in sorted(config.TRASH_DIR.iterdir(), reverse=True):
        manifest = bucket / MANIFEST
        if not manifest.is_file():
            continue
        try:
            entries.append(
                TrashEntry(**json.loads(manifest.read_text(encoding="utf-8")))
            )
        except (json.JSONDecodeError, TypeError, OSError):
            # A corrupt manifest should not take the whole listing down.
            continue
    return entries


def restore(entry_id: str) -> str:
    """Put an item back where it came from."""
    bucket = config.TRASH_DIR / entry_id
    manifest = bucket / MANIFEST
    if not manifest.is_file():
        raise FileNotFoundError(f"no trash entry {entry_id}")

    entry = TrashEntry(**json.loads(manifest.read_text(encoding="utf-8")))
    original = Path(entry.original_path)
    payload = next((p for p in bucket.iterdir() if p.name != MANIFEST), None)
    if payload is None:
        raise FileNotFoundError(f"trash entry {entry_id} has no payload")

    if original.exists():
        # Never clobber a live file to undo a delete. That trades one
        # irreversible mistake for another.
        original = original.with_name(f"{original.stem}.restored{original.suffix}")

    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(payload), str(original))
    shutil.rmtree(bucket, ignore_errors=True)
    return str(original)


def empty_expired(retention_days: int | None = None) -> list[str]:
    """The only code in JARVIS that genuinely destroys a user file."""
    limit = (
        retention_days if retention_days is not None else config.TRASH_RETENTION_DAYS
    )
    removed: list[str] = []
    for entry in list_trash():
        if entry.age_days >= limit:
            shutil.rmtree(config.TRASH_DIR / entry.entry_id, ignore_errors=True)
            removed.append(entry.original_path)
    return removed


def trash_size_bytes() -> int:
    return sum(e.size_bytes for e in list_trash())
