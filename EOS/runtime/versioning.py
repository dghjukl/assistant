from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()


def get_version() -> str:
    version_file = ROOT / "VERSION"
    if version_file.is_file():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    try:
        return version("eos")
    except PackageNotFoundError:
        pass

    pyproject = ROOT / "pyproject.toml"
    if pyproject.is_file():
        try:
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"\'')
        except OSError:
            pass

    return "unknown"
