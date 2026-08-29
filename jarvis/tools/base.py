"""Tool registry: one decorator, and the schema writes itself.

The guide names vague descriptions as the #1 cause of an agent calling the
wrong tool. The #2 cause is subtler and entirely self-inflicted: the schema
and the function drift apart. You rename a parameter, forget the declaration,
and the model starts sending arguments your code silently ignores.

So declarations are never written by hand here. `@tool` reads the function's
type hints and docstring and generates the JSON Schema. Rename a parameter and
the schema changes with it, because there is only one source of truth.

    @tool(group="files", untrusted_output=True)
    def read_file(path: str, max_bytes: int = 4000) -> dict:
        '''Read a UTF-8 text file from the workspace.

        Use for source code, notes and config. Do not use for binaries, and do
        not use it to check whether a file exists -- call list_directory.

        Args:
            path: Path relative to the workspace folder.
            max_bytes: Truncate after this many bytes.
        '''

Descriptions come from the docstring, and the "do not use it for" sentence
matters as much as the rest. Telling the model when *not* to reach for a tool
is the most effective fix for selection errors.
"""
from __future__ import annotations

import inspect
import re
import typing
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin
from collections.abc import Callable

_PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


class ToolError(Exception):
    """Raised inside a tool to return a clean error to the model.

    Errors go back as data, not as a crash. A good agent reads
    {"error": ..., "hint": ...} and tries again, so always give a hint.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_result(self) -> dict:
        out: dict[str, str] = {"error": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


# ---------------------------------------------------------------- docstrings
_ARGS_HEADER = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$", re.M)
_ARG_LINE = re.compile(r"^\s{2,}(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$")


def _tidy(text: str) -> str:
    """Collapse a docstring block into prose the model can read in one go."""
    paragraphs = [
        " ".join(part.split()) for part in text.strip().split("\n\n") if part.strip()
    ]
    return " ".join(paragraphs).strip()


def _parse_docstring(func: Callable) -> tuple[str, dict[str, str]]:
    """Return (description, {param: description}) from a Google-style docstring."""
    raw = inspect.getdoc(func) or ""
    if not raw:
        return "", {}

    match = _ARGS_HEADER.search(raw)
    if not match:
        return _tidy(raw), {}

    description = _tidy(raw[: match.start()])
    params: dict[str, str] = {}
    current: str | None = None

    for line in raw[match.end():].splitlines():
        if not line.strip():
            continue
        # A new unindented section (Returns:, Raises:) ends the Args block.
        if re.match(r"^\s{0,1}\w[\w ]*:\s*$", line):
            break
        arg_match = _ARG_LINE.match(line)
        if arg_match:
            current = arg_match.group(1).lstrip("*")
            params[current] = arg_match.group(2).strip()
        elif current:
            params[current] += " " + line.strip()
    return description, params


# ---------------------------------------------------------------- schema
def _schema_for(annotation: Any, description: str = "") -> dict:
    """Map one Python annotation to a JSON Schema fragment."""
    schema: dict[str, Any] = {}

    if annotation is inspect.Parameter.empty or annotation is Any:
        schema["type"] = "string"
    elif annotation in _PRIMITIVES:
        schema["type"] = _PRIMITIVES[annotation]
    else:
        origin = get_origin(annotation)
        args = get_args(annotation)

        if origin is Literal:
            # Enums beat free-text strings for tool-selection accuracy, so
            # Literal is the preferred way to declare a constrained argument.
            schema["type"] = _PRIMITIVES.get(type(args[0]), "string")
            schema["enum"] = list(args)
        elif origin in (list, typing.List):
            schema["type"] = "array"
            schema["items"] = _schema_for(args[0]) if args else {"type": "string"}
        elif origin in (dict, typing.Dict):
            schema["type"] = "object"
        elif origin is typing.Union or str(origin) == "types.UnionType":
            # Optional[X] is Union[X, None]. Describe X; optionality is
            # expressed by leaving the name out of `required`.
            concrete = [a for a in args if a is not type(None)]
            schema = _schema_for(concrete[0]) if concrete else {"type": "string"}
        else:
            schema["type"] = "string"

    if description:
        schema["description"] = description
    return schema


@dataclass
class ToolSpec:
    name: str
    func: Callable
    description: str
    parameters: dict
    group: str = "core"
    untrusted_output: bool = False
    always_available: bool = False

    def declaration(self) -> dict:
        """The JSON the model actually sees. It never sees your code."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class Registry:
    """Holds every tool, and decides which subset is active.

    Selection accuracy drops noticeably past roughly 20 tools -- the model
    starts picking plausible-but-wrong ones. `activate` exists so the full
    catalogue can be large while the set offered on any given turn stays
    small.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._active_groups: set[str] | None = None
        self._active_names: set[str] | None = None

    # ------------------------------------------------------------ register
    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._tools[spec.name] = spec

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def groups(self) -> list[str]:
        return sorted({t.group for t in self._tools.values()})

    # ------------------------------------------------------------ selection
    def activate(self, groups: set[str] | None) -> None:
        """Limit the offered set to these groups. None means everything."""
        self._active_groups = groups
        self._active_names = None

    def activate_names(self, names: set[str] | None) -> None:
        """Limit the offered set to exactly these tools.

        Groups turned out to be too coarse for the everyday loadout: keeping
        "files" whole drags trash management and move_path into every turn,
        and the offered set drifts past twenty, which is precisely where
        selection accuracy starts to slide. Naming tools explicitly keeps the
        default tight without shrinking the catalogue.
        """
        self._active_names = names
        self._active_groups = None

    def active(self) -> list[ToolSpec]:
        specs = list(self._tools.values())
        if self._active_names is not None:
            return [
                s
                for s in specs
                if s.name in self._active_names or s.always_available
            ]
        if self._active_groups is None:
            return specs
        return [
            s for s in specs if s.group in self._active_groups or s.always_available
        ]

    def select(self, names: set[str]) -> list[ToolSpec]:
        """Resolve a tool set *without touching shared state*.

        `activate`/`active` mutate the registry, which is fine for a REPL
        command but not for a turn: APScheduler runs background jobs on their
        own thread, and an audit found a job firing mid-conversation rewrote
        the active set out from under the user's turn -- 150 of 150 turns were
        sent the wrong toolset. Anything on a request path uses this instead.
        """
        return [
            s for s in self._tools.values() if s.name in names or s.always_available
        ]

    def declarations(
        self, extra: list[dict] | None = None, names: set[str] | None = None
    ) -> list[dict]:
        """Tool declarations for the API call.

        `names` selects explicitly and is thread-safe; omitting it falls back
        to the mutable active set, which only the REPL should rely on.

        `extra` carries Gemini's own built-ins, e.g. {"type": "google_search"},
        which are passed alongside your functions rather than implemented.
        """
        specs = self.select(names) if names is not None else self.active()
        decls = [s.declaration() for s in specs]
        if extra:
            decls.extend(extra)
        return decls

    def untrusted(self) -> set[str]:
        return {s.name for s in self._tools.values() if s.untrusted_output}

    # ------------------------------------------------------------ execute
    def dispatch(self, name: str, arguments: dict | None = None) -> dict:
        """Run a tool and always return a dict.

        Never raises. A tool that explodes returns its exception as data so
        the model can read it and recover, which is the difference between an
        agent that adapts and one that dies.
        """
        spec = self._tools.get(name)
        if spec is None:
            close = [n for n in self._tools if n.startswith(name[:4])]
            return {
                "error": f"unknown tool: {name}",
                "hint": (
                    f"did you mean one of {close}?" if close else "check the tool list"
                ),
            }

        arguments = dict(arguments or {})

        # Validate before executing. A clear schema error costs one cheap turn;
        # a TypeError deep inside the tool costs a confusing one.
        signature = inspect.signature(spec.func)
        allowed = set(signature.parameters)
        unexpected = set(arguments) - allowed
        if unexpected:
            return {
                "error": f"unexpected arguments: {sorted(unexpected)}",
                "hint": f"{name} accepts only {sorted(allowed)}",
            }

        missing = [
            p.name
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            and p.name not in arguments
        ]
        if missing:
            return {
                "error": f"missing required arguments: {missing}",
                "hint": f"{name} requires {missing}",
            }

        try:
            result = spec.func(**arguments)
        except ToolError as exc:
            return exc.as_result()
        except Exception as exc:  # noqa: BLE001 - deliberate: errors are data
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "the tool failed; try a different approach or tell the user",
            }

        return result if isinstance(result, dict) else {"result": result}


registry = Registry()


def tool(
    _func: Callable | None = None,
    *,
    name: str | None = None,
    group: str = "core",
    untrusted_output: bool = False,
    always_available: bool = False,
) -> Callable:
    """Register a function as a tool, deriving its schema from its signature.

    Args:
        name: Override the tool name. Defaults to the function name.
        group: Used by `Registry.activate` to swap tool sets by context.
        untrusted_output: True if this tool returns content JARVIS did not
            author. Its results get taint-scanned and fenced before they reach
            the model.
        always_available: Offered even when its group is inactive. For things
            like get_time that the model needs constantly.
    """

    def decorate(func: Callable) -> Callable:
        description, param_docs = _parse_docstring(func)
        if not description:
            raise ValueError(
                f"tool {func.__name__} has no docstring; the model sees only "
                "the description, so an undocumented tool is an unusable one"
            )

        hints = typing.get_type_hints(func)
        signature = inspect.signature(func)

        properties: dict[str, dict] = {}
        required: list[str] = []

        for param in signature.parameters.values():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            annotation = hints.get(param.name, param.annotation)
            properties[param.name] = _schema_for(
                annotation, param_docs.get(param.name, "")
            )
            if param.default is inspect.Parameter.empty:
                required.append(param.name)

        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required

        registry.add(
            ToolSpec(
                name=name or func.__name__,
                func=func,
                description=description,
                parameters=parameters,
                group=group,
                untrusted_output=untrusted_output,
                always_available=always_available,
            )
        )
        return func

    return decorate(_func) if _func is not None else decorate
