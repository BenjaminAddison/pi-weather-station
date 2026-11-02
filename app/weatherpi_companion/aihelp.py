"""Build the "Get AI help" pack: base instructions + live diagnostics + host checks +
the owner's description, all passed through :func:`redact` before it's shown.

The output is Markdown text the owner can edit, copy, or save as
``weatherpi-ai-help-<stamp>.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------- redaction

#: dict keys whose values are always secret (compared lower-case, '-'/' ' -> '_')
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pass", "pwd", "new_password", "old_password", "recovery_password",
    "psk", "wpa_psk", "passphrase", "wpa_passphrase", "wifi_password", "secret",
    "client_secret", "token", "api_token", "new_api_token", "access_token", "auth_token",
    "refresh_token", "bearer", "authorization", "auth_key", "authkey", "auth_url", "authurl",
    "private_key", "privatekey", "id_ed25519", "ssh_private_key", "cookie", "set_cookie",
    "802_11_wireless_security.psk", "wep_key0", "leap_password", "otp",
})

_SECRET_WORDS = (
    r"(?:new_|old_|recovery_|wifi_|wpa_|api_|access_|auth_|refresh_|client_)?"
    r"(?:password|passwd|passphrase|psk|secret|token|auth_?key)"
)

#: skip values an earlier pattern already replaced (avoids "[REDACTED]]")
_NOT_DONE = r"(?!(?:tskey-|https://login\.tailscale\.com/a/)?\[REDACTED\])"

# (pattern, replacement) applied in order
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM / OpenSSH private key blocks
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----"
                r"|\Z)", re.DOTALL), "-----[PRIVATE KEY REDACTED]-----"),
    # Tailscale auth keys / API keys
    (re.compile(r"\btskey-[A-Za-z0-9_-]+"), "tskey-" + REDACTED),
    # Tailscale login URLs (whoever opens one can join the device to *their* tailnet)
    (re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+"),
     "https://login.tailscale.com/a/" + REDACTED),
    # HTTP auth headers
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)\s+[^\s\"',;]+"),
     r"\1" + REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # credentials inside URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@"), r"\1" + REDACTED + "@"),
    # nmcli: 802-11-wireless-security.psk:  value
    (re.compile(r"(?im)(802-11-wireless-security\.(?:psk|wep-key\d|leap-password)\s*[:=]\s*)"
                r"(?!--\s*$)\S.*$"), r"\1" + REDACTED),
    # JSON-style "key": "value"
    (re.compile(r"(?i)(\"[\w.-]*" + _SECRET_WORDS + r"[\w.-]*\"\s*:\s*)\"(?:[^\"\\]|\\.)*\""),
     r'\1"' + REDACTED + '"'),
    # key=value / key: value (yaml, ini, wpa_supplicant, env, CLI flags)
    (re.compile(r"(?im)(^|[\s,{;?&]|--)(" + r"[\w.-]*" + _SECRET_WORDS + r"[\w.-]*"
                r")(\s*[:=]\s*)" + _NOT_DONE + r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&}\]\"']+)"),
     r"\1\2\3" + REDACTED),
    # prose: "my password is hunter22" / "the wifi password was: abc"
    (re.compile(r"(?i)\b((?:pass(?:word|phrase|code)?|pw|psk)\s+(?:is|was|=)\s*:?\s*)"
                + _NOT_DONE + r"(\"[^\"\n]+\"|'[^'\n]+'|\S+)"), r"\1" + REDACTED),
]

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RECOVERY_LIKE_RE = re.compile(r"\b[a-z2-9]{5}(?:-[a-z2-9]{5}){4}\b")


def _norm_key(k: Any) -> str:
    return str(k).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_key(k: Any) -> bool:
    n = _norm_key(k)
    if n in SENSITIVE_KEYS:
        return True
    return bool(re.search(r"(password|passphrase|secret|private_?key|(^|_)psk$|"
                          r"(^|_)token$|auth_?key$)", n))


def redact_obj(obj: Any) -> Any:
    """Deep copy with sensitive dict values replaced (strings are redacted too)."""
    if isinstance(obj, Mapping):
        out = {}
        for k, v in obj.items():
            if _is_sensitive_key(k) and v not in (None, "", [], {}):
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, list | tuple):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_text(
    text: str,
    known_secrets: Iterable[str] = (),
    *,
    hide_emails: bool = False,
) -> str:
    """Remove secrets from free text.

    ``known_secrets`` are exact values (API token, recovery password, owner password if the
    UI has it in memory ...) that are replaced wherever they appear.
    """
    if not text:
        return text
    # exact known values first, longest first so substrings don't leave fragments
    for s in sorted({s for s in known_secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, REDACTED)
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    text = _RECOVERY_LIKE_RE.sub(REDACTED, text)
    if hide_emails:
        text = _EMAIL_RE.sub("[email hidden]", text)
    return text


def redact(value: Any, known_secrets: Iterable[str] = (), *, hide_emails: bool = False) -> Any:
    """Redact a string or a JSON-like structure."""
    known = list(known_secrets)
    if isinstance(value, str):
        return redact_text(value, known, hide_emails=hide_emails)
    cleaned = redact_obj(value)
    text = json.dumps(cleaned, default=str)
    text = redact_text(text, known, hide_emails=hide_emails)
    return json.loads(text)


# ---------------------------------------------------------------------------- the pack


def base_instructions() -> str:
    return resources.files("weatherpi_companion.resources").joinpath(
        "ai_base.md").read_text(encoding="utf-8")


def _to_plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        d = asdict(obj)
        d.pop("raw", None)
        return d
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_to_plain(v) for v in obj]
    return obj


def _json_block(obj: Any) -> str:
    return "```json\n" + json.dumps(_to_plain(obj), indent=2, default=str,
                                    ensure_ascii=False) + "\n```"


def _section_diagnostics(diag: Mapping[str, Any]) -> str:
    """Pretty-print the agent's diagnostics; journals as text blocks, the rest as JSON."""
    diag = dict(diag)
    parts = []
    journal = diag.pop("journal", None)
    rest = {k: v for k, v in diag.items()}
    parts.append(_json_block(rest))
    if isinstance(journal, Mapping):
        for unit, lines in journal.items():
            parts.append(f"#### Journal: {unit}\n\n```text\n{str(lines).rstrip()}\n```")
    return "\n\n".join(parts)


def build_help_pack(
    *,
    owner_description: str = "",
    diagnostics: Mapping[str, Any] | None = None,
    diagnostics_error: str | None = None,
    host_checks: Any = None,
    station: Mapping[str, Any] | None = None,
    app_log_tail: str = "",
    known_secrets: Iterable[str] = (),
    hide_emails: bool = True,
    now: datetime | None = None,
    base_text: str | None = None,
) -> str:
    """Assemble and redact the pack. Every input is optional.

    ``host_checks`` may be a list of :class:`~weatherpi_companion.flows.CheckResult`, a
    dict, or anything JSON-able. ``station`` is e.g. the profile dict / ``/v1/info``.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    base = (base_text if base_text is not None else base_instructions()).rstrip()
    sections = [
        f"# The owner's situation (collected {stamp})",
        "## What the owner says is wrong",
        owner_description.strip() or "_(The owner didn't describe the problem.)_",
    ]
    if station:
        sections += ["## Station", _json_block(station)]
    sections.append("## Checks on the owner's computer")
    if host_checks:
        if isinstance(host_checks, list | tuple):
            lines = []
            for c in host_checks:
                c = _to_plain(c)
                if isinstance(c, Mapping) and "ok" in c:
                    mark = "PASS" if c.get("ok") else "FAIL"
                    detail = f" ({c.get('detail')})" if c.get("detail") else ""
                    lines.append(f"- {mark} `{c.get('code')}`: {c.get('message_for_owner', '')}"
                                 f"{detail}")
                else:
                    lines.append(f"- {json.dumps(c, default=str)}")
            sections.append("\n".join(lines))
        else:
            sections.append(_json_block(host_checks))
    else:
        sections.append("_(No host checks were run.)_")
    sections.append("## Live diagnostics from the WeatherPi")
    if diagnostics:
        sections.append(_section_diagnostics(diagnostics))
    else:
        sections.append("_(The app couldn't reach the WeatherPi"
                        + (f": {diagnostics_error}" if diagnostics_error else "") + ".)_")
    if app_log_tail.strip():
        sections += ["## Recent Companion app log", "```text\n" + app_log_tail.rstrip()
                     + "\n```"]
    live = "\n\n".join(sections) + "\n"
    # JSON blocks get the key-based pass (secret-named keys blanked whatever their value),
    # then everything gets the text patterns. The static base text is ours, so it's left as is.
    live = redact_text(_redact_json_blocks(live), known_secrets, hide_emails=hide_emails)
    return f"{base}\n\n---\n\n{live}"


_JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _redact_json_blocks(text: str) -> str:
    def fix(m: re.Match[str]) -> str:
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            return m.group(0)
        return "```json\n" + json.dumps(redact_obj(obj), indent=2, ensure_ascii=False) + "\n```"

    return _JSON_BLOCK_RE.sub(fix, text)


def pack_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    return f"weatherpi-ai-help-{stamp}.md"


def save_pack(text: str, folder: Path | str, now: datetime | None = None) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / pack_filename(now)
    path.write_text(text, encoding="utf-8")
    return path
