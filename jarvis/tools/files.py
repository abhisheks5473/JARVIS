"""Files, sandboxed to one folder. Always.

Every path the model supplies is resolved and checked against the workspace
root before anything opens it. The check is on the *resolved* path, so
"../../etc/passwd", a symlink, and an absolute path all fail the same way.

Deletion never destroys. `delete_path` moves into a recoverable trash which a
scheduled job empties after thirty days.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import config
from ..security import trash
from .base import ToolError, tool

# Reading a 400MB log into a prompt is a quota accident, not a feature.
MAX_READ_BYTES = 100_000
TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".log", ".html", ".css", ".xml", ".sql",
    ".sh", ".ps1", ".bat", ".env", ".gitignore", ".rst", ".java", ".c", ".h",
    ".cpp", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".r", ".jsonl",
}


def _resolve(path: str) -> Path:
    """Resolve `path` inside the workspace, or refuse.

    Resolution happens first and the comparison is on the real path, so
    traversal and symlink escapes are caught by the same check.
    """
    root = config.WORKSPACE.resolve()
    candidate = (
        Path(path).resolve() if Path(path).is_absolute() else (root / path).resolve()
    )

    if candidate != root and root not in candidate.parents:
        raise ToolError(
            "path is outside the workspace",
            hint=(
                f"only paths under {root.name}/ are reachable; give a path "
                "relative to the workspace, such as notes/todo.txt"
            ),
        )
    return candidate


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(config.WORKSPACE.resolve()))
    except ValueError:
        return str(path)


@tool(group="files", untrusted_output=True)
def read_file(path: str, max_bytes: int = 8000) -> dict:
    """Read a text file from the workspace folder.

    Use for source code, notes, configuration and data files. Do not use it
    for binaries or images. Do not use it just to check whether a file exists
    -- call list_directory for that, which is cheaper and will not fail.

    Args:
        path: Path relative to the workspace folder, e.g. notes/todo.txt
        max_bytes: Stop after this many bytes. Raise it only if the file was
            genuinely truncated and you need the rest.
    """
    target = _resolve(path)
    if not target.exists():
        raise ToolError(
            f"no such file: {_relative(target)}",
            hint="call list_directory to see what is actually there",
        )
    if target.is_dir():
        raise ToolError(
            f"{_relative(target)} is a directory",
            hint="use list_directory for directories",
        )

    limit = max(256, min(int(max_bytes), MAX_READ_BYTES))
    raw = target.read_bytes()[: limit + 1]
    truncated = len(raw) > limit
    try:
        content = raw[:limit].decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"{_relative(target)} is not UTF-8 text",
            hint="this looks like a binary file; read_file only handles text",
        ) from None

    return {
        "path": _relative(target),
        "content": content,
        "truncated": truncated,
        "size_bytes": target.stat().st_size,
    }


@tool(group="files")
def write_file(path: str, content: str, mode: str = "overwrite") -> dict:
    """Write text to a file in the workspace folder.

    Read the file first if it already exists -- never overwrite a path you
    have not seen. Requires the user's approval before it runs.

    Args:
        path: Path relative to the workspace folder.
        content: The full text to write.
        mode: Either overwrite or append.
    """
    if mode not in ("overwrite", "append"):
        raise ToolError("mode must be overwrite or append", hint="use one of those two")

    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()

    if mode == "append" and existed:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")

    return {
        "path": _relative(target),
        "bytes_written": len(content.encode("utf-8")),
        "created": not existed,
        "mode": mode,
    }


@tool(group="files")
def list_directory(path: str = ".", pattern: str = "*") -> dict:
    """List the contents of a workspace directory.

    Cheap, and it stops you guessing filenames. Call this before read_file
    whenever you are not certain a path exists.

    Args:
        path: Directory relative to the workspace folder. Defaults to the root.
        pattern: Glob filter such as *.py or notes*.
    """
    target = _resolve(path)
    if not target.exists():
        raise ToolError(
            f"no such directory: {_relative(target)}",
            hint="try listing the workspace root with path='.'",
        )
    if not target.is_dir():
        raise ToolError(f"{_relative(target)} is a file", hint="use read_file instead")

    entries = []
    for child in sorted(
        target.glob(pattern), key=lambda p: (p.is_file(), p.name.lower())
    ):
        try:
            entries.append(
                {
                    "name": child.name,
                    "kind": "dir" if child.is_dir() else "file",
                    "size_bytes": child.stat().st_size if child.is_file() else None,
                }
            )
        except OSError:
            continue

    return {
        "path": _relative(target),
        "entries": entries[:200],
        "count": len(entries),
        "truncated": len(entries) > 200,
    }


@tool(group="files", untrusted_output=True)
def search_files(query: str, path: str = ".", max_results: int = 25) -> dict:
    """Search text files in the workspace for a literal string.

    Use this to find where something is mentioned when you do not know the
    filename. Case-insensitive, literal, not a regular expression.

    Args:
        query: The text to look for.
        path: Directory to search under, relative to the workspace.
        max_results: Stop after this many matching lines.
    """
    if not query.strip():
        raise ToolError("query is empty", hint="give some text to search for")

    root = _resolve(path)
    needle = query.lower()
    hits: list[dict] = []

    for candidate in root.rglob("*"):
        if len(hits) >= max_results:
            break
        if not candidate.is_file() or candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size > MAX_READ_BYTES * 5:
                continue
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(
                        {
                            "file": _relative(candidate),
                            "line": number,
                            "text": line.strip()[:200],
                        }
                    )
                    if len(hits) >= max_results:
                        break
        except OSError:
            continue

    return {"query": query, "matches": hits, "count": len(hits)}


@tool(group="files")
def delete_path(path: str) -> dict:
    """Move a file or folder to the recoverable trash.

    This does not destroy anything. The item goes to a trash folder and is
    purged automatically after thirty days, so a mistake here is undoable.
    Requires the user's approval before it runs.

    Args:
        path: Path relative to the workspace folder.
    """
    target = _resolve(path)
    try:
        entry = trash.send_to_trash(target)
    except FileNotFoundError:
        raise ToolError(
            f"nothing at {_relative(target)}",
            hint="call list_directory first to confirm the path",
        ) from None

    return {
        "moved_to_trash": _relative(target),
        "trash_id": entry.entry_id,
        "recoverable_for_days": config.TRASH_RETENTION_DAYS,
        "note": "not deleted, recoverable",
    }


@tool(group="files")
def move_path(path: str, destination: str) -> dict:
    """Move or rename a file or folder inside the workspace.

    Both paths must be inside the workspace. Requires the user's approval.

    Args:
        path: Existing path, relative to the workspace folder.
        destination: New path, relative to the workspace folder.
    """
    source = _resolve(path)
    target = _resolve(destination)

    if not source.exists():
        raise ToolError(
            f"nothing at {_relative(source)}", hint="check with list_directory"
        )
    if target.exists():
        raise ToolError(
            f"{_relative(target)} already exists",
            hint="pick a different destination, or delete the existing one first",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return {"moved": _relative(source), "to": _relative(target)}


@tool(group="files")
def list_trash() -> dict:
    """List everything currently in the recoverable trash.

    Use this when the user asks about something they deleted, or wants
    something restored.
    """
    entries = trash.list_trash()
    return {
        "entries": [
            {
                "trash_id": e.entry_id,
                "original_path": e.original_path,
                "deleted": e.deleted_at_iso,
                "expires_in_days": round(e.expires_in_days, 1),
            }
            for e in entries[:50]
        ],
        "count": len(entries),
    }


@tool(group="files")
def restore_from_trash(trash_id: str) -> dict:
    """Restore a trashed item to where it came from.

    Args:
        trash_id: The trash_id reported by list_trash or delete_path.
    """
    try:
        restored = trash.restore(trash_id)
    except FileNotFoundError as exc:
        raise ToolError(
            str(exc), hint="call list_trash to see valid trash ids"
        ) from None
    return {"restored_to": restored}
