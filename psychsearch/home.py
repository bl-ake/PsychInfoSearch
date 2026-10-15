"""The folder psychsearch works in: settings (.env), results, the response cache and saved terms files.

It is ~/Documents/psychsearch unless PSYCHSEARCH_HOME names another one (set it to "." to work in the
current folder). Each command moves into it on start, so every relative path it writes lands there.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT = Path.home() / "Documents" / "psychsearch"


def folder() -> Path:
    value = os.environ.get("PSYCHSEARCH_HOME", "").strip()
    return Path(value).expanduser().resolve() if value else DEFAULT


def find(path: Path) -> Path:
    """A file named on the command line: where it is from the current folder if it exists there,
    otherwise inside the psychsearch folder. Call before `enter`."""
    if path.is_absolute():
        return path
    here = Path.cwd() / path
    return here if here.exists() else folder() / path


def enter() -> Path:
    path = folder()
    path.mkdir(parents=True, exist_ok=True)
    os.chdir(path)
    return path
