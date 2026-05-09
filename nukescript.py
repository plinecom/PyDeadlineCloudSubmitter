"""Minimal Nuke `.nk` script parser.

Extracts the Nuke version (from the `version` line near the top of the file),
the project frame range / fps / format (Root node knobs), and the list of
Write / Read nodes — all without requiring Nuke or any third-party packages.

`.nk` files are plain text in a Tcl-like syntax:

    #! /usr/local/Nuke15.1v5/Nuke15.1 -nx
    version 15.1 v5
    Root {
     inputs 0
     name /path/to/script.nk
     frame 1
     first_frame 1
     last_frame 100
     fps 24
     format "1920 1080 0 0 1920 1080 1 HD_1080"
     views { main }
    }
    Read {
     inputs 0
     file "/path/to/seq.%04d.exr"
     name Read1
    }
    Write {
     file "/path/to/out.%04d.exr"
     file_type exr
     name Write1
    }

Usage:
    from nukescript import read_nukescript_info, collect_external_refs
    info = read_nukescript_info("path/to/script.nk")
    refs = collect_external_refs("path/to/script.nk")

CLI smoke test:
    python nukescript.py path/to/script.nk [--refs]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from blendfile import ExternalRef


# --------------------------------------------------------------------------
# Public dataclasses
# --------------------------------------------------------------------------
@dataclass
class NukeWriteNode:
    name: str
    file: str               # raw `file` knob value (may contain patterns / Tcl)
    file_type: str          # e.g. "exr", "mov"
    channels: str = ""      # e.g. "rgba", "rgb"
    views: str = ""         # views knob (empty = inherit from Root)
    disable: bool = False   # `disable true` knob


@dataclass
class NukeFileInfo:
    version: str                          # e.g. "15.1"
    subversion: str                       # e.g. "v5"
    start_frame: int                      # Root.first_frame (or .frame fallback)
    end_frame: int                        # Root.last_frame  (or .frame fallback)
    fps: float                            # Root.fps (0.0 if absent)
    format_name: str                      # last token of Root.format (e.g. "HD_1080")
    format_resolution: str                # e.g. "1920x1080"
    views: List[str] = field(default_factory=list)        # Root.views
    write_nodes: List[NukeWriteNode] = field(default_factory=list)


# --------------------------------------------------------------------------
# Tokenizer / brace matcher
# --------------------------------------------------------------------------
def _scan_balanced_braces(text: str, start: int) -> int:
    """Given text[start] == '{', return the index of the matching '}'.

    Tracks double-quoted strings (with backslash escapes) so that braces
    inside string literals don't confuse the depth counter.
    """
    if text[start] != "{":
        raise ValueError(f"Expected '{{' at {start}, got {text[start]!r}")
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # Consume the quoted string.
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i - 1
        else:
            i += 1
    raise ValueError(f"Unbalanced '{{' starting at {start}")


_NODE_HEADER_RE = re.compile(
    r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*\{",
    re.MULTILINE,
)


def _iter_blocks(text: str) -> Iterable[Tuple[str, str]]:
    """Yield (node_type, body_text) for every brace-block in `text`.

    Walks the entire text and recurses into block bodies, so blocks nested
    inside a Group (or similar container) are also yielded. Blocks that
    appear as a knob value (e.g. ``metadata { ... }`` inside another node)
    are also yielded, but their type names rarely collide with the
    user-visible node types we care about (Root / Read / Write / ...).
    """
    pos = 0
    n = len(text)
    while pos < n:
        m = _NODE_HEADER_RE.search(text, pos)
        if not m:
            return
        node_type = m.group(1)
        brace_pos = m.end() - 1
        try:
            end = _scan_balanced_braces(text, brace_pos)
        except ValueError:
            return
        body = text[brace_pos + 1:end]
        yield node_type, body
        # Recurse into the body so nested blocks are visible too.
        for sub in _iter_blocks(body):
            yield sub
        pos = end + 1


def _parse_knobs(body: str) -> dict[str, str]:
    """Parse a node body into a {knob_name: raw_value_text} dict.

    The value runs from after the knob name to the end of the line, OR — if
    the value opens a `{` — until the matching `}` is closed. This handles
    multi-line knobs like ``metadata { ... }``. Quoted strings are respected.
    """
    knobs: dict[str, str] = {}
    pos = 0
    n = len(body)
    while pos < n:
        # Skip whitespace / blank lines.
        while pos < n and body[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            break
        # Skip Tcl line comments.
        if body[pos] == "#":
            while pos < n and body[pos] != "\n":
                pos += 1
            continue
        # Read the knob name (identifier).
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", body[pos:])
        if not m:
            # Unrecognized token: skip to next whitespace.
            while pos < n and body[pos] not in " \t\r\n":
                pos += 1
            continue
        knob_name = m.group()
        pos += len(knob_name)
        # Inline whitespace before the value.
        while pos < n and body[pos] in " \t":
            pos += 1
        # Read the value: everything until newline at brace depth 0,
        # or until a balanced { ... } closes if the value opens braces.
        val_start = pos
        depth = 0
        while pos < n:
            c = body[pos]
            if c == '"':
                pos += 1
                while pos < n:
                    if body[pos] == "\\" and pos + 1 < n:
                        pos += 2
                        continue
                    if body[pos] == '"':
                        pos += 1
                        break
                    pos += 1
            elif c == "{":
                depth += 1
                pos += 1
            elif c == "}":
                if depth == 0:
                    break
                depth -= 1
                pos += 1
            elif c == "\n" and depth == 0:
                break
            else:
                pos += 1
        knobs[knob_name] = body[val_start:pos].strip()
    return knobs


def _unquote(value: str) -> str:
    """Strip an outer pair of double quotes (and unescape \\" / \\\\) if present."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
        v = v.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
    return v


def _parse_braced_list(value: str) -> List[str]:
    """Parse a Tcl-style braced list like ``{ main left right }`` into items."""
    v = value.strip()
    if v.startswith("{") and v.endswith("}"):
        v = v[1:-1]
    return [tok for tok in v.replace("\n", " ").split() if tok]


def _parse_views_knob(value: str) -> List[str]:
    """Parse Root.views into a list of view names.

    Nuke historically writes views as ``{ {main} {left rgba} {right rgba} }``
    where each inner brace pair is ``{view_name [channels]}``. We only want
    the view name (first token of each pair).
    """
    v = value.strip()
    if not v:
        return []
    # Find each inner {...} pair; if there are none, fall back to a flat list.
    pairs = re.findall(r"\{([^{}]*)\}", v)
    if pairs:
        return [p.split()[0] for p in pairs if p.split()]
    # Flat: "{ main left right }" or just "main left right"
    return _parse_braced_list(v)


def _parse_first_int(text: str, default: int = 0) -> int:
    """Return the first integer in `text` (e.g. ``1`` from ``"1.0"`` or ``" 100 "``)."""
    m = re.search(r"-?\d+", text)
    return int(m.group()) if m else default


# --------------------------------------------------------------------------
# Top-level: read script info
# --------------------------------------------------------------------------
_VERSION_LINE_RE = re.compile(
    r"^\s*version\s+([0-9.]+)\s*([Vv][0-9]+)?\s*$",
    re.MULTILINE,
)


def _read_text(path: Path) -> str:
    # .nk files are ASCII / UTF-8. Fall back to latin-1 so a stray non-UTF-8
    # byte (e.g. in a metadata blob) doesn't crash the parser.
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def read_nukescript_info(nk_path) -> NukeFileInfo:
    """Parse a .nk script into NukeFileInfo.

    Reads the whole file (.nk scripts are usually <10MB) and walks every
    brace block. The first ``Root { ... }`` provides project-wide settings;
    every ``Write { ... }`` becomes an entry in `write_nodes`.
    """
    nk_path = Path(nk_path)
    text = _read_text(nk_path)

    # ---- Version line ----
    version = ""
    subversion = ""
    m = _VERSION_LINE_RE.search(text)
    if m:
        version = m.group(1)
        subversion = (m.group(2) or "").lower()

    # ---- Walk blocks ----
    start_frame = 1
    end_frame = 1
    fps = 0.0
    format_name = ""
    format_resolution = ""
    views: List[str] = []
    write_nodes: List[NukeWriteNode] = []

    saw_root = False
    for node_type, body in _iter_blocks(text):
        if node_type == "Root" and not saw_root:
            saw_root = True
            knobs = _parse_knobs(body)
            # Frame range: prefer first_frame/last_frame, fall back to .frame.
            if "first_frame" in knobs:
                start_frame = _parse_first_int(knobs["first_frame"], 1)
            elif "frame" in knobs:
                start_frame = _parse_first_int(knobs["frame"], 1)
            if "last_frame" in knobs:
                end_frame = _parse_first_int(knobs["last_frame"], start_frame)
            else:
                end_frame = start_frame
            if "fps" in knobs:
                try:
                    fps = float(_unquote(knobs["fps"]))
                except ValueError:
                    fps = 0.0
            if "format" in knobs:
                # Root.format = "W H X1 Y1 X2 Y2 PA Name" — last token is name.
                fmt_str = _unquote(knobs["format"])
                tokens = fmt_str.split()
                if len(tokens) >= 2:
                    try:
                        format_resolution = f"{int(tokens[0])}x{int(tokens[1])}"
                    except ValueError:
                        format_resolution = ""
                if tokens:
                    # Last token is the name if it's not numeric.
                    last = tokens[-1]
                    if not re.match(r"^-?\d+(\.\d+)?$", last):
                        format_name = last
            if "views" in knobs:
                views = _parse_views_knob(knobs["views"])
        elif node_type == "Write":
            knobs = _parse_knobs(body)
            write_nodes.append(
                NukeWriteNode(
                    name=_unquote(knobs.get("name", "")),
                    file=_unquote(knobs.get("file", "")),
                    file_type=_unquote(knobs.get("file_type", "")),
                    channels=_unquote(knobs.get("channels", "")),
                    views=_unquote(knobs.get("views", "")),
                    disable=_unquote(knobs.get("disable", "")).lower()
                    in ("true", "1"),
                )
            )

    return NukeFileInfo(
        version=version,
        subversion=subversion,
        start_frame=start_frame,
        end_frame=end_frame,
        fps=fps,
        format_name=format_name,
        format_resolution=format_resolution,
        views=views,
        write_nodes=write_nodes,
    )


# --------------------------------------------------------------------------
# External file references (Read nodes)
# --------------------------------------------------------------------------
# Knobs that hold a path on disk for various file-reading node types.
# Value is the knob name we read; nodes not in this dict are ignored.
_FILE_REF_KNOBS = {
    "Read":         "file",
    "DeepRead":     "file",
    "ReadGeo":      "file",
    "ReadGeo2":     "file",
    "Camera":       "file",  # Camera.file (alembic / fbx)
    "Camera2":      "file",
    "Camera3":      "file",
    "Axis":         "file",
    "Axis2":        "file",
    "Axis3":        "file",
    "Light":        "file",
    "OCIOFileTransform": "file",
    "Vectorfield":  "vfield_file",
    "AudioRead":    "file",
}


def _expand_pattern(pattern_path: Path) -> List[Path]:
    """Expand a Nuke file pattern (printf or hash style) into actual files.

    Handles ``%04d`` / ``%d`` (printf-style numeric padding) and ``####``
    (hash-style padding). Returns sorted matching files; if the pattern
    contains no recognized placeholder the path is returned as-is when the
    file exists, otherwise an empty list.
    """
    parent = pattern_path.parent
    name = pattern_path.name

    # If there's no recognized frame placeholder, treat as a single file.
    has_pattern = bool(re.search(r"%0?\d*d|#+", name))
    if not has_pattern:
        return [pattern_path] if pattern_path.is_file() else []

    if not parent.is_dir():
        return []

    # Build a regex that matches the pattern's filename, capturing the
    # frame digits. Everything else is escaped so dots / brackets / etc.
    # are matched literally.
    parts: list[str] = []
    i = 0
    while i < len(name):
        c = name[i]
        if c == "#":
            count = 0
            while i < len(name) and name[i] == "#":
                count += 1
                i += 1
            parts.append(rf"\d{{{count}}}")
        elif c == "%":
            m = re.match(r"%0?(\d*)d", name[i:])
            if m:
                width = m.group(1)
                if width:
                    parts.append(rf"\d{{{int(width)}}}")
                else:
                    parts.append(r"\d+")
                i += m.end()
            else:
                parts.append(re.escape(c))
                i += 1
        else:
            parts.append(re.escape(c))
            i += 1

    name_re = re.compile("^" + "".join(parts) + "$")
    matches = [p for p in parent.iterdir() if p.is_file() and name_re.match(p.name)]
    return sorted(matches)


def _resolve_nuke_path(raw: str, referrer: Path) -> Optional[Path]:
    """Resolve a raw Nuke file knob value to an absolute path.

    Returns None if the value contains a Tcl/Python expression we can't
    resolve at submit time (``[python ...]``, ``[getenv ...]``, etc.).
    Relative paths are resolved against the referrer's parent directory
    (Nuke's `nuke.script_directory()` semantics).
    """
    if not raw:
        return None
    # Tcl/Python expressions — too dynamic to resolve statically.
    if "[" in raw and "]" in raw:
        return None
    # Environment variable references (Bash / Tcl style).
    if "$" in raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = referrer.parent / p
    return p.resolve()


def collect_external_refs(nk_path) -> List[ExternalRef]:
    """Collect external file references from a .nk script.

    Walks every Read-like node, expands frame patterns into the actual
    files on disk, and returns one `ExternalRef` per resolved file.
    Patterns that match no files contribute a single ExternalRef with
    `exists=False` so the caller can warn about missing inputs.

    Unlike the Blender side this does NOT recurse: a Read of an .abc / .nk
    file is treated as a leaf reference.
    """
    nk_path = Path(nk_path).resolve()
    if not nk_path.is_file():
        return []
    text = _read_text(nk_path)

    refs: list[ExternalRef] = []
    seen: set[Path] = set()

    for node_type, body in _iter_blocks(text):
        knob_name = _FILE_REF_KNOBS.get(node_type)
        if not knob_name:
            continue
        knobs = _parse_knobs(body)
        raw = _unquote(knobs.get(knob_name, ""))
        if not raw:
            continue
        resolved = _resolve_nuke_path(raw, nk_path)
        if resolved is None:
            # Unresolvable expression — record as a single missing entry so
            # the user sees it in the linked-files panel.
            refs.append(
                ExternalRef(
                    referrer=nk_path,
                    type=node_type,
                    raw=raw,
                    resolved=Path(raw),
                    exists=False,
                )
            )
            continue
        expanded = _expand_pattern(resolved)
        if not expanded:
            if resolved not in seen:
                seen.add(resolved)
                refs.append(
                    ExternalRef(
                        referrer=nk_path,
                        type=node_type,
                        raw=raw,
                        resolved=resolved,
                        exists=False,
                    )
                )
            continue
        for f in expanded:
            if f in seen:
                continue
            seen.add(f)
            refs.append(
                ExternalRef(
                    referrer=nk_path,
                    type=node_type,
                    raw=raw,
                    resolved=f,
                    exists=True,
                )
            )
    return refs


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} path/to/script.nk [--refs]", file=sys.stderr)
        sys.exit(2)
    info = read_nukescript_info(sys.argv[1])
    print(
        f"version={info.version} {info.subversion}  "
        f"frames={info.start_frame}-{info.end_frame}  fps={info.fps}"
    )
    print(f"format={info.format_resolution} ({info.format_name or '-'})")
    print(f"views={info.views}")
    for w in info.write_nodes:
        flag = " (disabled)" if w.disable else ""
        print(f"  Write {w.name}: {w.file_type}  file={w.file!r}{flag}")
    if "--refs" in sys.argv[2:]:
        refs = collect_external_refs(sys.argv[1])
        present = sum(1 for r in refs if r.exists)
        print(f"external_refs: {len(refs)} total / {len(refs) - present} missing")
        for r in refs:
            mark = "OK" if r.exists else "MISSING"
            print(f"  [{mark:7}] {r.type:14} {r.resolved}")
