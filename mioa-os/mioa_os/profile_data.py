"""Trusted-local Profile data reset; no managed confinement is implied."""
from pathlib import Path, PurePath
import shutil


class LocalProfileDataReset:
    """Reset a Host-selected Profile directory using local pathname authority."""

    def reset(self, directory: str | PurePath) -> None:
        directory = Path(directory)
        data = directory / "data"
        if data.is_dir():
            shutil.rmtree(data)
        data.mkdir(parents=True, exist_ok=True)
        for name in ("engine.log", "engine.sock", "state.json"):
            (directory / name).unlink(missing_ok=True)
