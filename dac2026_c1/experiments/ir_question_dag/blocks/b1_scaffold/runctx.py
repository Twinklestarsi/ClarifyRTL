"""Per-run directory, event log and artifact hashing.

A run directory is the only place results live::

    runs/<dataset>__<short>/<run_id>/
        run_config.json
        events.jsonl
        artifacts.json
        phase_a/ ... dag/ ... rounds/ ... derived/ ... rtl/ ... formal/
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunContext:
    """Owns one case run: directories, events, artifact hash registry."""

    def __init__(self, directory: Path, config: dict[str, Any], case_key: str):
        self.directory = Path(directory)
        self.config = config
        self.case_key = case_key
        self.started = time.time()
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.events_path = self.directory / "events.jsonl"
        self.artifacts_path = self.directory / "artifacts.json"
        self.stage_name: str | None = None

    # ------------------------------------------------------------------ setup
    @classmethod
    def start(cls, *, output_root: Path | str, case_key: str,
              config: dict[str, Any], run_id: str | None = None) -> "RunContext":
        run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        directory = Path(output_root) / case_key / run_id
        directory.mkdir(parents=True, exist_ok=True)
        ctx = cls(directory, config, case_key)
        ctx.write_json("run_config.json", {
            "case": case_key, "run_id": run_id, "started": _now(),
            "config": {k: v for k, v in config.items() if not k.startswith("_")},
            "config_source": config.get("_source"),
        })
        ctx.event("run_started", case=case_key, run_id=run_id)
        return ctx

    # ------------------------------------------------------------------ basics
    def path(self, *parts: str) -> Path:
        target = self.directory.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_json(self, relative: str, value: Any, *, record: bool = True) -> Path:
        target = self.path(relative)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        if record:
            self.record(target)
        return target

    def read_json(self, relative: str, default: Any = None) -> Any:
        target = self.directory / relative
        if not target.is_file():
            return default
        return json.loads(target.read_text(encoding="utf-8"))

    def record(self, path: Path | str, **extra: Any) -> None:
        target = Path(path)
        if not target.is_file():
            return
        entry = {"path": str(target.relative_to(self.directory)),
                 "sha256": sha256_file(target), "bytes": target.stat().st_size,
                 "at": _now(), **extra}
        self.artifacts[entry["path"]] = entry
        self.artifacts_path.write_text(
            json.dumps(self.artifacts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    # ------------------------------------------------------------------ events
    def event(self, kind: str, **payload: Any) -> None:
        record = {"ts": _now(), "kind": kind, "case": self.case_key,
                  "stage": self.stage_name, "elapsed_s": round(time.time() - self.started, 2),
                  **payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @contextmanager
    def stage(self, name: str):
        """Time a stage and record failures without swallowing them."""
        self.stage_name = name
        started = time.time()
        self.event("stage_started", stage=name)
        try:
            yield self
        except Exception as exc:
            self.event("stage_failed", stage=name, error=type(exc).__name__,
                       message=str(exc)[:2000],
                       traceback=traceback.format_exc()[-4000:],
                       duration_s=round(time.time() - started, 2))
            raise
        else:
            self.event("stage_finished", stage=name,
                       duration_s=round(time.time() - started, 2))
        finally:
            self.stage_name = None

    def finish(self, status: str, **payload: Any) -> dict[str, Any]:
        summary = {"case": self.case_key, "status": status, "finished": _now(),
                   "duration_s": round(time.time() - self.started, 2),
                   "directory": str(self.directory), **payload}
        self.write_json("result.json", summary)
        self.event("run_finished", status=status, duration_s=summary["duration_s"])
        return summary