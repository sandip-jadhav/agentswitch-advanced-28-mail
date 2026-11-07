"""Login, token loading and identity (SD-D-3, SD-D-10).

* `login`: password via getpass (never stored), POST /api/auth/login, `{token, signature}` into
  `~/.config/agentswitch/<system>.json` with mode 0600.
* `load`: harness token, else `AGENTSWITCH_<SYSTEM>_TOKEN`, else the login file.
* `load_signature`: `BRIEFING_SIGNATURE`, else the signature in the login file (never in harness mode).
* `whoami`: GET /api/auth/me -> `User`. Failures are the typed errors of `mcp.py`; the user-facing
  lines come from `mcp.failure_message`.
"""
import getpass as _getpass
import http.client
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import SIGNATURE_VAR
from .mcp import DEFAULT_TIMEOUT, LoginExpired, ToolFailed, Unreachable, urllib_transport


class LoginInputMissing(Exception):
    """Login input (the email, or the signature on first login) could not be read: end of input or blank."""


class LoginRejected(Exception):
    """The platform refused the email or password. Never carries the password."""


@dataclass(frozen=True)
class User:
    id: str
    employee_id: str  # None when the platform sends none
    email: str
    name: str


def config_dir(env):
    home = (env.get("HOME") or "").strip()
    return (Path(home) if home else Path.home()) / ".config" / "agentswitch"


def token_path(system_name, env):
    return config_dir(env) / (system_name + ".json")


def _read_file(system_name, env):
    try:
        data = json.loads(token_path(system_name, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(system_name, mode, env):
    """The token to use for a system, or None. Harness mode reads no file and no per-system variable."""
    if mode == "harness":
        return (env.get("AGENTSWITCH_TOKEN") or "").strip() or None
    token = (env.get("AGENTSWITCH_%s_TOKEN" % system_name.upper()) or "").strip()
    if token:
        return token
    token = _read_file(system_name, env).get("token")
    return token if isinstance(token, str) and token else None


def load_signature(system_name, mode, env):
    """The name replies are signed with: BRIEFING_SIGNATURE, else the one given at login for this system
    (local mode only; harness mode reads no file), else None (the caller uses the /api/auth/me name)."""
    chosen = (env.get(SIGNATURE_VAR) or "").strip()
    if chosen:
        return chosen
    if mode == "harness":
        return None
    signature = _read_file(system_name, env).get("signature")
    return signature.strip() if isinstance(signature, str) and signature.strip() else None


def _send(transport, method, url, headers, body, timeout):
    try:
        return (transport or urllib_transport)(method, url, headers, body, timeout)
    except (OSError, http.client.HTTPException) as e:
        raise Unreachable(type(e).__name__ + (": " + str(e) if str(e) else "")) from None


def _json_object(text):
    try:
        data = json.loads(text)
    except ValueError:
        raise ToolFailed("unreadable reply from the platform") from None
    if not isinstance(data, dict):
        raise ToolFailed("unexpected reply from the platform")
    return data


def _text(value):
    return None if value is None or value == "" else str(value)


def whoami(base_url, token, transport=None, timeout=DEFAULT_TIMEOUT):
    """The login's identity from /api/auth/me."""
    if not token:
        raise LoginExpired()
    status, _, text = _send(transport, "GET", base_url.rstrip("/") + "/api/auth/me",
                            {"Accept": "application/json", "Authorization": "Bearer " + token}, None, timeout)
    if status == 401:
        raise LoginExpired()
    if not 200 <= status < 300:
        raise ToolFailed("HTTP %d" % status)
    data = _json_object(text)
    for key in ("user", "data"):  # tolerate the user sitting under a key (shape unverified on Suryodaya)
        if isinstance(data.get(key), dict):
            data = data[key]
            break
    user_id = _text(data.get("id"))
    if user_id is None:
        raise ToolFailed("the platform's identity reply has no user id")
    return User(user_id, _text(data.get("employee_id")), _text(data.get("email")) or "", _text(data.get("name")) or "")


def _write_private(path, content):
    """Write `content` to `path` with mode 0600 via a temporary file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def login(system, env, transport=None, getpass_fn=None, input_fn=None, timeout=DEFAULT_TIMEOUT):
    """Ask for email and password, log in, and store the token (and, first time only, the signature).

    `system` has `.name` and `.base_url`.
    """
    getpass_fn = getpass_fn or _getpass.getpass
    input_fn = input_fn or input

    def ask(prompt, what):
        try:
            answer = input_fn(prompt).strip()
        except EOFError:
            answer = ""
        if not answer:
            raise LoginInputMissing("Could not read %s — run 'briefing.py login %s' again in a terminal"
                                    % (what, system.name))
        return answer

    email = ask("Email: ", "your email")
    signature = _read_file(system.name, env).get("signature")
    if not isinstance(signature, str) or not signature.strip():
        signature = ask("Name to sign replies with: ", "the name to sign replies with")  # first login only
    password = getpass_fn("Password: ")
    status, _, text = _send(transport, "POST", system.base_url.rstrip("/") + "/api/auth/login",
                            {"Content-Type": "application/json", "Accept": "application/json"},
                            json.dumps({"email": email, "password": password}).encode(), timeout)
    del password
    if status in (400, 401, 403):
        raise LoginRejected("The email or password was not accepted — check them and try again")
    if not 200 <= status < 300:
        raise ToolFailed("HTTP %d" % status)
    token = _json_object(text).get("token")
    if not isinstance(token, str) or not token:
        raise ToolFailed("the login reply has no token")
    _write_private(token_path(system.name, env), json.dumps({"token": token, "signature": signature}))
