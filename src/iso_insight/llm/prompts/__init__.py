"""Versioned prompts (CLAUDE.md: prompts are versioned files).

Each prompt lives in its own file, ``<name>_v<version>.md``, in this
package — the version is part of the filename, so a changed prompt is a
new file, never an edit of an existing one. Results carry the prompt
version, which is what makes them reproducible.

The structured-output shape (the "Zwróć JSON w formacie" block) is part
of the prompt file itself; :func:`load_prompt` returns the newest
version present for a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Prompt:
    """A named, versioned prompt."""

    name: str
    version: int
    text: str


def _prompt_paths(name: str) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in _PROMPTS_DIR.glob(f"{name}_v*.md"):
        suffix = path.stem.rsplit("_v", 1)[1]
        if suffix.isdigit():
            found.append((int(suffix), path))
    return found


@lru_cache
def load_prompt(name: str) -> Prompt:
    """Load the newest version of the named prompt.

    Raises:
        FileNotFoundError: when no ``<name>_v<version>.md`` exists.
    """
    found = _prompt_paths(name)
    if not found:
        raise FileNotFoundError(f"prompt not found: {name!r} in {_PROMPTS_DIR}")
    version, path = max(found)
    return Prompt(name=name, version=version, text=path.read_text(encoding="utf-8").strip())
