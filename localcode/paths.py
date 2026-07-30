from __future__ import annotations

import os
from pathlib import Path

APP_ID = "io.localcode.LocalCode"
APP_NAME = "LocalCode"
PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent


def _xdg_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else fallback


def data_dir() -> Path:
    override = os.environ.get("LOCALCODE_DATA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / "localcode"


def config_dir() -> Path:
    override = os.environ.get("LOCALCODE_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config") / "localcode"


def cache_dir() -> Path:
    override = os.environ.get("LOCALCODE_CACHE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache") / "localcode"


def database_path() -> Path:
    return data_dir() / "localcode.db"


def transcript_dir(project_id: str) -> Path:
    return data_dir() / "transcripts" / project_id


def palace_path() -> Path:
    return data_dir() / "mempalace" / "palace"


def bundled_mempalace_root() -> Path | None:
    candidates = (
        SOURCE_ROOT / "vendor" / "mempalace",
        data_dir() / "app" / "vendor" / "mempalace",
    )
    return next((path for path in candidates if (path / "pyproject.toml").is_file()), None)


def ensure_app_dirs() -> None:
    for path in (data_dir(), config_dir(), cache_dir(), data_dir() / "transcripts"):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
