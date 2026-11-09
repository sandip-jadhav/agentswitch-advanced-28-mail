"""Settings for one run: the system table, zones, harness mode and environment (G3-AC1, G6-AC1).

`resolve(args, env)` never reads `os.environ`; the caller passes the environment. Problems the user
must fix raise `ConfigError` (exit code 2). Notes and warnings are plain lines for the caller to show.
"""
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """A usage or configuration error: one plain line, exit code 2."""


@dataclass(frozen=True)
class SystemInfo:
    name: str
    label: str
    base_url: str
    default_zone: str
    default_zone_label: str  # how the fallback message names the default zone


SYSTEMS = {
    "suryodaya": SystemInfo("suryodaya", "Suryodaya", "https://agentswitch.theschoolofai.in",
                            "Asia/Kolkata", "IST"),
    "keystone": SystemInfo("keystone", "Keystone", "https://class.agentswitch.theschoolofai.in",
                           "America/New_York", "US Eastern"),
}
SYSTEM_ORDER = ("suryodaya", "keystone")

HARNESS_VARS = ("AGENTSWITCH_TOKEN", "AGENTSWITCH_INSTANCE", "AGENTSWITCH_BASE_URL")

# ---- The model endpoint (G6-AC2, SD-D-2). glc is not OpenAI-compatible (plan task 15 probe): `draft_writer` tries
# `<url>/chat/completions`, then glc's own `<url>/chat`. The names and the default live only here.
DEFAULT_LLM_URL = "http://127.0.0.1:8111/v1"
NO_MODEL_WARNING = "BRIEFING_LLM_MODEL not set — no drafts this run"
SIGNATURE_VAR = "BRIEFING_SIGNATURE"  # read by auth.load_signature
DEFAULT_BRIEFING_DIR = str(Path(__file__).resolve().parent.parent / "briefings")


@dataclass(frozen=True)
class SystemSettings:
    name: str
    label: str
    base_url: str
    zone: ZoneInfo            # the display zone: the platform's, or the --tz override
    storage_zone: ZoneInfo    # the platform's own zone, which its offset-less times are local to


@dataclass(frozen=True)
class ModelSettings:
    url: str                # the base the endpoint hangs off, no trailing slash: `<url>/chat/completions`
    model: str              # None when unset: the run then makes no model call
    api_key: str = None     # sent as `Authorization: Bearer …` when set; a secret, never printed

    def __repr__(self):     # keep the key out of any traceback or log line
        return "ModelSettings(url=%r, model=%r, api_key=%s)" % (self.url, self.model,
                                                                "***" if self.api_key else None)


@dataclass
class Settings:
    mode: str                       # "local" or "harness"
    systems: list                   # [SystemSettings] to read, in display order
    briefing_dir: str
    notes: list = field(default_factory=list)      # lines the user sees (unknown time zone)
    warnings: list = field(default_factory=list)   # lines for standard error
    model: ModelSettings = None     # where drafts come from (both modes)


def _get(env, name):
    return (env.get(name) or "").strip()


def _parse_tz(options):
    """['keystone=Europe/Amsterdam', ...] -> {'keystone': 'Europe/Amsterdam'}"""
    wanted = {}
    for option in options or []:
        name, sep, zone = option.partition("=")
        name = name.strip().lower()
        if not sep or not name:
            raise ConfigError("--tz needs system=zone, for example --tz keystone=Europe/Amsterdam")
        if name not in SYSTEMS:
            raise ConfigError("--tz names an unknown system '%s' — use suryodaya or keystone" % name)
        wanted[name] = zone.strip()
    return wanted


def _zone(info, requested, notes):
    """The ZoneInfo for a system: the requested zone, or the default with a note."""
    if requested is None:
        return ZoneInfo(info.default_zone)
    try:
        if not requested:
            raise ValueError("empty")
        return ZoneInfo(requested)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        notes.append("Unknown time zone '%s' — using %s for %s" % (requested, info.default_zone_label, info.label))
        return ZoneInfo(info.default_zone)


def _http_url(value, name):
    if not value.lower().startswith(("http://", "https://")):
        raise ConfigError("%s must start with http:// or https://" % name)
    return value.rstrip("/")


def _model_settings(env, warnings):
    """OPENAI_* when OPENAI_BASE_URL is set (the model is then required), else BRIEFING_LLM_* with glc's
    default URL; other OPENAI_* variables are ignored then. An unset BRIEFING_LLM_MODEL is not an error:
    the run goes on without drafts and says so."""
    base = _get(env, "OPENAI_BASE_URL")
    if base:
        model = _get(env, "OPENAI_MODEL")
        if not model:
            raise ConfigError("OPENAI_MODEL is required when OPENAI_BASE_URL is set")
        return ModelSettings(_http_url(base, "OPENAI_BASE_URL"), model, _get(env, "OPENAI_API_KEY") or None)
    model = _get(env, "BRIEFING_LLM_MODEL") or None
    if model is None:
        warnings.append(NO_MODEL_WARNING)
    url = _get(env, "BRIEFING_LLM_URL")
    return ModelSettings(_http_url(url, "BRIEFING_LLM_URL") if url else DEFAULT_LLM_URL, model)


def resolve(args, env):
    """Decide mode, systems, zones and folders from the parsed arguments and the environment.

    `args` has `.system` (None or a system name) and `.tz` (None or a list of 'system=zone').
    """
    system_arg = getattr(args, "system", None)
    system_arg = system_arg.strip().lower() if system_arg else None
    if system_arg is not None and system_arg not in SYSTEMS:
        raise ConfigError("Unknown system '%s' — use suryodaya or keystone" % system_arg)
    tz = _parse_tz(getattr(args, "tz", None))
    briefing_dir = _get(env, "BRIEFING_DIR") or DEFAULT_BRIEFING_DIR
    notes, warnings = [], []

    token, instance, base = (_get(env, v) for v in HARNESS_VARS)
    if token or instance:
        missing = [v for v, value in zip(HARNESS_VARS, (token, instance, base)) if not value]
        if missing:
            raise ConfigError("Harness mode needs AGENTSWITCH_TOKEN, AGENTSWITCH_INSTANCE and "
                              "AGENTSWITCH_BASE_URL together — missing: " + ", ".join(missing))
        if not base.lower().startswith(("http://", "https://")):
            raise ConfigError("AGENTSWITCH_BASE_URL must start with http:// or https://")
        instance = instance.lower()
        if instance not in SYSTEMS:
            raise ConfigError("AGENTSWITCH_INSTANCE '%s' is not suryodaya or keystone" % instance)
        if system_arg is not None and system_arg != instance:
            raise ConfigError("--system %s conflicts with AGENTSWITCH_INSTANCE=%s" % (system_arg, instance))
        info = SYSTEMS[instance]
        system = SystemSettings(info.name, info.label, base.rstrip("/"), _zone(info, tz.get(instance), notes),
                                ZoneInfo(info.default_zone))
        return Settings("harness", [system], briefing_dir, notes=notes,
                        warnings=warnings, model=_model_settings(env, warnings))

    if base:
        warnings.append("AGENTSWITCH_BASE_URL is ignored unless AGENTSWITCH_TOKEN and "
                        "AGENTSWITCH_INSTANCE are set too")
    names = [system_arg] if system_arg else list(SYSTEM_ORDER)
    systems = []
    for name in names:
        info = SYSTEMS[name]
        systems.append(SystemSettings(info.name, info.label, info.base_url, _zone(info, tz.get(name), notes),
                                      ZoneInfo(info.default_zone)))
    return Settings("local", systems, briefing_dir, notes=notes, warnings=warnings,
                    model=_model_settings(env, warnings))
