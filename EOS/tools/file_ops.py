"""Tool: File Operations — Read, write, list files within allowed paths."""
from __future__ import annotations

from pathlib import Path


def _project_root(cfg: dict) -> Path:
    """Derive the project root from db_path config (one level up from data/)."""
    return Path(cfg.get("db_path", "data/entity_state.db")).parent.parent.resolve()


def _allowed_roots(cfg: dict) -> list[Path]:
    return [Path.home(), _project_root(cfg)]


def _check_allowed(path: Path, cfg: dict) -> None:
    resolved = path.resolve()
    for root in _allowed_roots(cfg):
        try:
            resolved.relative_to(root.resolve())
            return
        except ValueError:
            continue
    raise PermissionError(f"Path not in allowed directories: {path}")


def read_file(path: str, cfg: dict) -> str:
    p = Path(path)
    _check_allowed(p, cfg)
    if not p.exists():
        return f"File not found: {path}"
    return p.read_text(encoding="utf-8", errors="replace")


def write_file(path: str, content: str, cfg: dict) -> str:
    p = Path(path)
    _check_allowed(p, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Written: {path}"


def list_dir(path: str, cfg: dict) -> str:
    p = Path(path)
    _check_allowed(p, cfg)
    if not p.is_dir():
        return f"Not a directory: {path}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = [f"  [{'FILE' if e.is_file() else 'DIR '}] {e.name}" for e in entries]
    return f"Contents of {path}:\n" + "\n".join(lines)


def append_file(path: str, content: str, cfg: dict) -> str:
    p = Path(path)
    _check_allowed(p, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content)
    return f"Appended to: {path}"
