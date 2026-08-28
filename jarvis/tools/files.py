"""Files, confined to a short list of folders.

Every path the model supplies is resolved and checked against the allowed
roots before anything opens it. The check is on the *resolved* path, so
"../../etc/passwd", a symlink, and an absolute path all fail the same way.

The roots are the workspace plus your ordinary document folders (Desktop,
Documents, Downloads by default). A single workspace was the original design
and it was too narrow: "put a file on my desktop" is most of what a desktop
assistant is asked to do, and refusing it made the tool useless for the job.

Widening the roots created a new problem -- this project lives on the Desktop,
so its own .env came into range. Hence the denylist in `_is_sensitive`, which
is independent of the roots and refuses credential files and protected
directories wherever they appear.

Deletion never destroys. `delete_path` moves into a recoverable trash which a
scheduled job empties after thirty days.
"""
from __future__ import annotations

import shutil
from fnmatch import fnmatch
from pathlib import Path

from .. import config
from ..security import trash
from ..security.approval import redact
from .base import ToolError, tool

# Reading a 400MB log into a prompt is a quota accident, not a feature.
MAX_READ_BYTES = 100_000

# Desktop can contain an entire node_modules or site-packages tree. Walking it
# unbounded made search_files take minutes and read third-party source.
MAX_FILES_SCANNED = 4000
PRUNED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", "site-packages", "dist-info", ".idea", ".vscode",
    "AppData", "$RECYCLE.BIN", "System Volume Information",
}


def _walk(root: Path):
    """Yield files under `root`, skipping trees nobody means to search."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            for child in current.iterdir():
                if child.is_dir():
                    if child.name in PRUNED_DIRS or child.name.startswith("."):
                        continue
                    stack.append(child)
                else:
                    yield child
        except (OSError, PermissionError):
            continue
TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".log", ".html", ".css", ".xml", ".sql",
    ".sh", ".ps1", ".bat", ".gitignore", ".rst", ".java", ".c", ".h",
    ".cpp", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".r", ".jsonl",
}


def _is_sensitive(candidate: Path) -> str | None:
    """Return a reason if this path must stay unreachable, else None.

    Allowing Desktop as a root would otherwise expose this project's own .env,
    since JARVIS lives on the Desktop. This denylist is checked on every
    access and is independent of which root the file sits under.
    """
    name = candidate.name.lower()
    for pattern in config.SENSITIVE_FILE_PATTERNS:
        if fnmatch(name, pattern.lower()):
            return f"{candidate.name} is a credential file"

    parts = {p.lower() for p in candidate.parts}
    for blocked in config.SENSITIVE_DIR_NAMES:
        if blocked.lower() in parts:
            return f"{blocked} is a protected directory"

    # The agent's own state is not the agent's business. Note the equality
    # check: a directory is not in its own `parents`, so an earlier version
    # blocked data/memory.db but happily listed data/ itself.
    try:
        data = config.DATA_DIR.resolve()
        if candidate == data or data in candidate.parents:
            return "that is the private data directory"
    except OSError:
        pass
    return None


def _anchor(path: str, roots: list[Path], workspace: Path) -> Path:
    """Turn whatever the model wrote into a real absolute path.

    Models say "~/Desktop/notes.txt", "Desktop/notes.txt", and
    "C:/Users/abhis/Desktop/notes.txt" interchangeably. On a machine where
    OneDrive has redirected the Desktop, only one of those is a real location
    and the other two point at a folder that does not exist. Rather than
    refuse on a technicality, a leading known-folder name is matched by name
    against the allowed roots and re-anchored to wherever that folder actually
    lives.

    This cannot widen access: the result is still checked against `roots` by
    the caller, and re-anchoring only ever maps onto a root that is already
    allowed.
    """
    cleaned = path.strip().strip('"').replace("\\", "/").lstrip("~").lstrip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]

    if parts:
        head = parts[0].lower()
        for root in roots:
            if root.name.lower() == head:
                return (root.joinpath(*parts[1:]) if len(parts) > 1 else root).resolve()

    raw = Path(path).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
        # An absolute path under the home directory naming a known folder that
        # does not exist there is almost certainly a redirected folder.
        try:
            relative = resolved.relative_to(Path.home())
        except ValueError:
            return resolved
        if relative.parts:
            head = relative.parts[0].lower()
            for root in roots:
                if root.name.lower() == head and not resolved.exists():
                    return root.joinpath(*relative.parts[1:]).resolve()
        return resolved

    return (workspace / raw).resolve()


def _resolve(path: str) -> Path:
    """Resolve `path` against the allowed roots, or refuse.

    Resolution happens first and every comparison is on the real path, so
    traversal ("../../Windows") and symlink escapes are caught by the same
    check. Relative paths are interpreted against the workspace, which keeps
    the common case short.
    """
    roots = [r.resolve() for r in config.ALLOWED_ROOTS]
    workspace = config.WORKSPACE.resolve()

    candidate = _anchor(path, roots, workspace)

    inside = any(candidate == root or root in candidate.parents for root in roots)
    if not inside:
        readable = ", ".join(r.name or str(r) for r in roots)
        raise ToolError(
            f"path is outside the folders I may touch: {candidate}",
            hint=(
                f"I can reach {readable}. Give a path inside one of those, or "
                "a bare filename, which lands in the workspace."
            ),
        )

    reason = _is_sensitive(candidate)
    if reason:
        raise ToolError(
            f"refusing to touch that path -- {reason}",
            hint=(
                "credential files and protected directories are permanently "
                "off-limits; tell the user rather than trying another route"
            ),
        )
    return candidate


def _relative(path: Path) -> str:
    """Shortest readable form: relative to whichever root contains it."""
    for root in config.ALLOWED_ROOTS:
        try:
            rel = path.relative_to(root.resolve())
        except ValueError:
            continue
        return str(rel) if root == config.WORKSPACE else f"{root.name}/{rel}"
    return str(path)


@tool(group="files", untrusted_output=True)
def read_file(path: str, max_bytes: int = 8000) -> dict:
    """Read a text file.

    You can reach the workspace, the Desktop, Documents and Downloads. Use for
    source code, notes, configuration and data files. Do not use it for
    binaries or images, and do not use it just to check whether a file exists
    -- call list_directory for that, which is cheaper and will not fail.

    Credential files such as .env or SSH keys are permanently refused. If you
    hit that, say so; there is no alternative route.

    Args:
        path: A bare name lands in the workspace. Otherwise give a folder,
            e.g. "Desktop/notes.txt" or "Documents/report.md".
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
    """Write text to a file.

    You can write to the workspace, the Desktop, Documents and Downloads, so
    "make a file on my desktop" is something you can simply do. Read the file
    first if it already exists -- never overwrite a path you have not seen.
    Requires the user's approval before it runs.

    Args:
        path: A bare name lands in the workspace. Otherwise give a folder,
            e.g. "Desktop/notes.txt".
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
    """List the contents of a directory.

    Cheap, and it stops you guessing filenames. Call this before read_file
    whenever you are not certain a path exists. Works on the workspace, the
    Desktop, Documents and Downloads.

    Args:
        path: Directory to list, e.g. "Desktop" or "." for the workspace.
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
        # Do not advertise what cannot be opened. Listing .env by name is a
        # smaller problem than reading it, but it is still a signpost.
        if _is_sensitive(child):
            continue
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
        path: Directory to search under, e.g. "." or "Desktop".
        max_results: Stop after this many matching lines.
    """
    if not query.strip():
        raise ToolError("query is empty", hint="give some text to search for")

    root = _resolve(path)
    needle = query.lower()
    hits: list[dict] = []
    scanned = 0

    # _resolve validates the root. It says nothing about the thousands of
    # files underneath it, and an audit found this walking straight into
    # client_secret.json and prod.env. Every file is checked individually.
    for candidate in _walk(root):
        if len(hits) >= max_results or scanned > MAX_FILES_SCANNED:
            break
        if not candidate.is_file() or candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if _is_sensitive(candidate):
            continue
        scanned += 1
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
                            "text": redact(line.strip()[:200]),
                        }
                    )
                    if len(hits) >= max_results:
                        break
        except OSError:
            continue

    return {
        "query": query,
        "matches": hits,
        "count": len(hits),
        "files_scanned": scanned,
        "truncated": scanned > MAX_FILES_SCANNED,
    }


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
