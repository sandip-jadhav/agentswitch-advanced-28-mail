"""What reaches the screen and the disk: redaction, file names, the nothing-fetched rule, atomic writes
(G1-AC4 to G1-AC6, G5-AC6, SD-D-12).

* `redact(text, secrets)` replaces every loaded token or key with `***`; `cli` uses it for the screen,
  `save` and `save_json` for the files. Redaction never stops a file from being saved.
* The Markdown file is `<date>.md` for a full run and `<date>-<system>.md` for one system, by the
  machine-local date. A run that fetched nothing writes no file and leaves the earlier one alone.
* Files are written to a temporary file in the same folder and renamed into place, so a failed write
  never damages an earlier file.
"""
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import SYSTEM_ORDER
from .render import nothing_fetched

STARS = "***"
NOTHING_FETCHED = "Nothing fetched — today's earlier briefing file kept"


@dataclass
class SaveOutcome:
    status: str          # "saved", "nothing_fetched" or "failed"
    path: Path = None
    message: str = None  # the line for the user when status is not "saved"


def _ordered(secrets):
    return sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True)


def redact(text, secrets):
    """`text` with every secret replaced by ***. Longer secrets go first, so one inside another is whole."""
    for secret in _ordered(secrets):
        text = text.replace(secret, STARS)
    return text


def redact_value(value, secrets):
    """The same for a JSON-like value: strings (and dict keys) anywhere inside it."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {redact(str(k), secrets): redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, secrets) for v in value]
    return value


def file_name(local_date, system_names):
    """`<date>.md` when every system was requested, else `<date>-<system>.md`."""
    names = list(system_names)
    if set(names) >= set(SYSTEM_ORDER):
        return "%s.md" % local_date.isoformat()
    return "%s-%s.md" % (local_date.isoformat(), "-".join(names))


def _atomic_write(path, text):
    """Write `text` to `path` through a temporary file in the same folder and os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _reason(exc):
    return exc.strerror or str(exc) or type(exc).__name__


def save(text, results, directory, local_date, system_names, secrets):
    """Save the briefing text unless nothing was fetched. Never raises on a file problem."""
    if nothing_fetched(results):
        return SaveOutcome("nothing_fetched", None, NOTHING_FETCHED)
    path = Path(directory) / file_name(local_date, system_names)
    try:
        _atomic_write(path, redact(text, secrets))
    except OSError as exc:
        return SaveOutcome("failed", path,
                           "Briefing shown but not saved to file: %s — copy it from the screen, or check that "
                           "the briefing folder exists and you can write to it, then rerun" % redact(_reason(exc), secrets))
    return SaveOutcome("saved", path)


def save_json(value, path, secrets):
    """Write the `--json` result, redacted. Never raises on a file problem."""
    path = Path(path)
    try:
        _atomic_write(path, json.dumps(redact_value(value, secrets), indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        return SaveOutcome("failed", path, "JSON result not saved to %s: %s" % (path, redact(_reason(exc), secrets)))
    return SaveOutcome("saved", path)
