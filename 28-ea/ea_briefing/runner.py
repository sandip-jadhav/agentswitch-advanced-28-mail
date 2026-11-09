"""One run: the timer, identity per system, and one thread per part (G1-AC1 to G1-AC3, SD-D-13).

Systems are read at the same time. Per system: `auth.whoami`, then `mail`, `meetings` and `tasks` each
in a thread with its own `McpClient`. If the identity can't be read, all three parts of that system
get the same plain reason. A reader that fails gives a missing part and never stops the others; only a
`ToolNotAllowed` (a programming error) is passed on.

Drafting (G2-AC2, G2-AC7, SD-D-13): `run` owns one queue shared by both systems. Each Mail part that was
read adds its messages (`on_mail`); 4 worker threads take the oldest `received_at` first and call
`draft_writer.write(..., deadline=)`. A worker takes nothing at or after start + 45 s, read only from
`clock.monotonic()`; the writer holds its own model calls (the redraft too) and its results to the same
deadline. After every join the outcome is attached to its message's `MailPart.drafts`: a message never taken
gets `DraftFailed("time limit")`, and a writer that raises, or a queue that broke, gives
`DraftFailed("model unavailable")`. Every such failure keeps the keyword mark (`keyword_marked`, G2-AC3).
"""
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from . import auth, mail, meetings, tasks
from .draft_writer import TIME_LIMIT, UNAVAILABLE, DraftFailed, keyword_marked
from .mcp import LoginExpired, McpClient, McpError, PartResult, ToolFailed, ToolNotAllowed, failure_message
from .render import SystemResult

WORKERS = 4
DRAFT_DEADLINE = 45.0   # seconds from the start of the run
_LATEST = datetime.max.replace(tzinfo=timezone.utc)


@dataclass
class RunResult:
    results: list      # [SystemResult], in the order of the systems asked for
    elapsed: float     # seconds on the clock's monotonic scale
    model: dict = None  # `draft_writer.stats()`: calls, failures, unreachable; None without a writer


def _guard(label, part, read):
    """The reader's PartResult; an unexpected error becomes a missing part, a forbidden tool is re-raised."""
    try:
        return read()
    except ToolNotAllowed:
        raise
    except Exception as exc:  # a reader bug must not hide the other parts (PRD-D-10)
        return PartResult.missing(failure_message(label, ToolFailed("unexpected error: " + type(exc).__name__), part))


def _tell(progress, line):
    """`progress(line)`; a broken stderr must never kill a worker or the run."""
    if progress:
        try:
            progress(line)
        except Exception:
            pass


class _DraftQueue:
    """The messages waiting for a draft, both systems together, and what the workers made of them."""

    def __init__(self, draft_writer, signatures, clock, deadline, progress):
        self._writer, self._signatures, self._clock = draft_writer, signatures or {}, clock
        self._deadline, self._progress = deadline, progress
        self._cond = threading.Condition()
        self._items = []          # (system, user, message), taken oldest received_at first
        self._closed = False
        self._total = self._done = 0
        self.outcomes = {}        # (system name, message id) -> Draft | DraftFailed

    def add(self, system, user, messages):
        """`on_mail`: queue the messages a Mail part says need an answer."""
        with self._cond:
            self._items.extend((system, user, message) for message in messages)
            self._total += len(messages)
            self._cond.notify_all()

    def drop(self, system):
        """Forget the messages of `system` still waiting (its login expired, so its Mail part is gone)."""
        with self._cond:
            kept = [item for item in self._items if item[0] is not system]
            self._total -= len(self._items) - len(kept)
            self._items = kept

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _take(self):
        """The oldest waiting item, or None when the workers should stop."""
        with self._cond:
            while not self._items and not self._closed:
                self._cond.wait()
            if not self._items or self._clock.monotonic() >= self._deadline:
                return None
            best = min(range(len(self._items)), key=lambda i: self._items[i][2].received or _LATEST)
            return self._items.pop(best)

    def _write(self, system, user, message):
        try:  # an unexpected writer error is one message's problem, never the run's (P-B-34)
            return self._writer.write(message, user, self._signatures.get(system.name), deadline=self._deadline)
        except Exception:
            return DraftFailed(UNAVAILABLE, None, keyword_marked(message))

    def work(self):
        while True:
            item = self._take()
            if item is None:
                return
            system, user, message = item
            outcome = self._write(system, user, message)
            with self._cond:
                self.outcomes[(system.name, message.id)] = outcome
                self._done += 1
                line = "Drafting replies (%d of %d)…" % (self._done, self._total)
            _tell(self._progress, line)  # outside the lock


def _read_system(system, token, clock, transport, fatal, on_mail=None, drop_mail=None):
    """The SystemResult of one system. Runs in its own thread.

    `on_mail(system, user, messages)` is called once with the messages needing an answer when the Mail
    part was read. (R2 draws `on_mail(system, needs_answer)`; the worker also needs the user, see the
    deviation log.) Drafting starts at once (G2-AC7); if the system's login turns out to be expired,
    `drop_mail(system)` takes its still-queued messages back out. A failing `on_mail` leaves its messages
    `DraftFailed("model unavailable")`.
    """
    label = system.label
    try:
        user = auth.whoami(system.base_url, token, transport)
    except McpError as exc:
        gone = PartResult.missing(failure_message(label, exc), isinstance(exc, LoginExpired))
        return SystemResult(system.name, label, gone, gone, gone)
    except ToolNotAllowed:
        raise
    except Exception as exc:  # a bug in the identity read must not stop the other system (PRD-D-10)
        gone = PartResult.missing(failure_message(label, ToolFailed("unexpected error: " + type(exc).__name__)))
        return SystemResult(system.name, label, gone, gone, gone)

    def client():
        return McpClient(system.base_url, token, transport, label=label)

    readers = {
        "mail": lambda: mail.read(client(), user),
        "meetings": lambda: meetings.read(client(), user, system.zone, clock, naive_zone=system.storage_zone),
        "tasks": lambda: tasks.read(client(), user, system.zone, clock),
    }
    parts = {}

    def work(name):
        try:
            parts[name] = _guard(label, name, readers[name])
            if name == "mail" and on_mail and parts[name].ok:
                try:
                    on_mail(system, user, parts[name].data.needs_answer)
                except Exception:  # a broken queue must not hide the briefing (PRD-D-10)
                    for message in parts[name].data.needs_answer:
                        parts[name].data.drafts.setdefault(
                            message.id, DraftFailed(UNAVAILABLE, None, keyword_marked(message)))
        except BaseException as exc:
            fatal.append(exc)

    threads = [threading.Thread(target=work, args=(name,), name="%s-%s" % (system.name, name)) for name in readers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if fatal:
        return None
    if any(parts[name].expired for name in readers):  # a 401 from any tool fails the whole system (P-B-2)
        if drop_mail:
            drop_mail(system)
        gone = PartResult.missing(failure_message(label, LoginExpired()), expired=True)
        return SystemResult(system.name, label, gone, gone, gone)
    return SystemResult(system.name, label, parts["mail"], parts["meetings"], parts["tasks"], system.zone)


def _attach(results, outcomes, missing):
    """Put each message's outcome on its MailPart. One with no outcome keeps what the part already holds
    (a failed `on_mail`), else gets `missing` (a DraftFailed reason) with the keyword mark."""
    for result in results:
        if result.mail.ok:
            drafts = result.mail.data.drafts
            for message in result.mail.data.needs_answer:
                drafts[message.id] = (outcomes.get((result.name, message.id)) or drafts.get(message.id)
                                      or DraftFailed(missing, None, keyword_marked(message)))


def run(systems, tokens, clock, transport=None, progress=None, draft_writer=None, signatures=None):
    """Read every system in `systems` ([config.SystemSettings]) with `tokens` ({name: token or None}).

    `progress(line)` gets "Reading <System>…" for each system before any platform call, and
    "Drafting replies (n of m)…" as replies finish. `draft_writer` writes the replies (None: every
    message is "model unavailable"); `signatures` is {system name: signature or None}.
    """
    started = clock.monotonic()
    slots = [None] * len(systems)
    fatal = []
    queue = _DraftQueue(draft_writer, signatures, clock, started + DRAFT_DEADLINE, progress) if draft_writer else None

    def work(i, system):
        try:
            slots[i] = _read_system(system, tokens.get(system.name), clock, transport, fatal,
                                    queue.add if queue else None, queue.drop if queue else None)
        except BaseException as exc:
            fatal.append(exc)

    workers = [threading.Thread(target=queue.work, name="draft-%d" % n) for n in range(WORKERS)] if queue else []
    for worker in workers:
        worker.start()
    threads = []
    for i, system in enumerate(systems):
        _tell(progress, "Reading %s…" % system.label)
        threads.append(threading.Thread(target=work, args=(i, system), name=system.name))
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        if queue:
            queue.close()
        for worker in workers:
            worker.join()
    if fatal:
        raise fatal[0]
    _attach(slots, queue.outcomes if queue else {}, TIME_LIMIT if queue else UNAVAILABLE)
    return RunResult(slots, clock.monotonic() - started, draft_writer.stats() if draft_writer else None)
