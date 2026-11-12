"""Ambient artifact discovery for an explicitly trusted local Host."""
import os
from pathlib import Path, PurePath
import stat


class LocalArtifactDiscovery:
    """Discover local candidates; this does not authorize or validate them."""

    def default_binary(self, runtime: str, repository_default: PurePath) -> str | PurePath:
        return os.environ.get(f"ANTI_TRACE_{runtime.upper()}_BINARY") or repository_default

    def font_paths(self) -> list[str]:
        directories = (Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                       Path.home() / "Library/Fonts")
        paths: set[str] = set()

        def skip_missing(error: OSError) -> None:
            if not isinstance(error, FileNotFoundError):
                raise error

        for directory in directories:
            try:
                mode = directory.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                continue
            for root, _, files in os.walk(directory, onerror=skip_missing):
                for name in files:
                    path = Path(root) / name
                    if path.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                        continue
                    try:
                        mode = path.stat().st_mode
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(mode):
                        paths.add(str(path.resolve()))
        return sorted(paths)
