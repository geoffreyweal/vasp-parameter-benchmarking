"""Minimal INCAR reader/editor.

Only one operation is needed: set an INCAR tag to a value, updating it in place
if the tag is already present (case-insensitively, preserving the surrounding
layout and any trailing comment) or appending it otherwise. VASP tag names are
case-insensitive; values are written verbatim so the caller controls formatting.
"""

from __future__ import annotations

import re
from pathlib import Path

# "  ENCUT = 400  ! plane-wave cutoff"  ->  indent, tag, sep, value, comment
_ASSIGN_RE = re.compile(
    r"^(?P<indent>\s*)(?P<tag>[A-Za-z0-9_]+)(?P<sep>\s*=\s*)(?P<value>.*?)"
    r"(?P<comment>\s*[!#].*)?$"
)


def set_tag(text: str, key: str, value: str) -> str:
    """Return ``text`` with INCAR tag ``key`` set to ``value``.

    The first line assigning ``key`` (case-insensitive) is rewritten, keeping
    its indentation, ``=`` spacing and trailing ``!``/``#`` comment. If no such
    line exists the tag is appended at the end.

    Lines packing several tags with ``;`` are not split apart - only single
    ``TAG = value`` lines are matched in place; anything else falls through to
    an appended line.
    """
    key_u = key.upper()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if ";" in line:  # multi-tag line: don't risk corrupting it
            continue
        m = _ASSIGN_RE.match(line)
        if m and m.group("tag").upper() == key_u:
            comment = m.group("comment") or ""
            lines[i] = f"{m.group('indent')}{m.group('tag')}{m.group('sep')}{value}{comment}"
            return "\n".join(lines) + "\n"

    # Not found - append, keeping a single trailing newline.
    while lines and lines[-1].strip() == "":
        lines.pop()
    lines.append(f"{key_u} = {value}")
    return "\n".join(lines) + "\n"


def set_tags(text: str, assignments: dict[str, str]) -> str:
    """Apply :func:`set_tag` for every ``key -> value`` in ``assignments``."""
    for key, value in assignments.items():
        text = set_tag(text, key, value)
    return text


def write_with_tags(path: str | Path, assignments: dict[str, str]) -> None:
    """Edit the INCAR at ``path`` in place, setting every given tag."""
    p = Path(path)
    p.write_text(set_tags(p.read_text(), assignments))
