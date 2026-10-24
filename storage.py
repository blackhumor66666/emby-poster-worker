"""Atomic JSON/text helpers shared by the worker modules."""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

LOG = logging.getLogger("poster.storage")


def load_json_file(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback
    except (OSError, ValueError, TypeError):
        LOG.warning("could not load JSON file %s", path)
        return fallback

def atomic_write_text(path: Path, content: str) -> None:
    """Write a text file atomically so a restart cannot leave partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_report(path: Path, fallback: Any) -> Any:
    return load_json_file(path, fallback)
