"""Non-secret code/environment metadata for comparing saved experiments."""

import platform
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def runtime_provenance() -> dict:
    versions = {}
    for package in ("adaptive-deep-research-agent", "langgraph", "pydantic",
                    "langchain", "langchain-deepseek", "langchain-anthropic"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            pass
    result = {"python": platform.python_version(), "packages": versions,
              "git_commit": None, "git_dirty": None}
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return result
    try:
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        result["git_commit"] = git("rev-parse", "HEAD")
        result["git_dirty"] = bool(git("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        pass
    return result
