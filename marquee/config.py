"""Configuration: environment variables first (GitHub Actions secrets land
there), then the gitignored .env file for local runs. Never commit keys.
"""
import os
import pathlib

_ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"


def get(name, default=None):
    if name in os.environ:
        return os.environ[name]
    if _ENV.exists():
        for line in _ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return default
