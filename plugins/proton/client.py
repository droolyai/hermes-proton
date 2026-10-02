"""Thin, honest wrapper around the `proton` CLI (roman-16/proton-cli).

Design notes
------------
* Every call runs with ``--output json --no-input``. ``--no-input`` means a
  missing or expired session becomes a clean error instead of the CLI blocking
  forever on an invisible password prompt.
* Exit codes are meaningful and are mapped, not swallowed:

  ==== ==========================================
  0    success
  1    something you passed was wrong
  2    authentication failed  (session expired)
  3    not found
  4    ambiguous / conflict
  5    network or server problem
  130  cancelled
  ==== ==========================================

* Nothing here ever writes a credential anywhere. Login is interactive and is
  the user's job (`proton account login`); this module only *reports* when the
  session is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

DEFAULT_TIMEOUT = 120

EXIT_MEANING = {
    0: "success",
    1: "bad argument",
    2: "authentication failed",
    3: "not found",
    4: "ambiguous or conflicting",
    5: "network or server problem",
    130: "cancelled",
}


class ProtonError(RuntimeError):
    """A proton-cli invocation failed."""

    def __init__(self, message: str, *, exit_code: int = -1, command: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.command = command
        self.meaning = EXIT_MEANING.get(exit_code, "unknown failure")

    @property
    def auth_required(self) -> bool:
        return self.exit_code == 2


def binary() -> str | None:
    """Absolute path to the proton binary, or None when it isn't installed."""
    candidate = os.path.expanduser("~/.hermes/bin/proton")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("proton")


def available() -> bool:
    return binary() is not None


def run(
    args: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    stdin: str | None = None,
) -> Any:
    """Run proton-cli and return parsed JSON.

    ``stdin`` feeds data to the process rather than the command line, which
    keeps message bodies out of the system process list where any local user
    could read them via ``ps``.

    Raises ProtonError on any non-zero exit, carrying the code and its meaning.
    """
    exe = binary()
    if exe is None:
        raise ProtonError(
            "proton-cli is not installed. Build it with: "
            "git clone https://github.com/roman-16/proton-cli ~/proton-cli && "
            "cd ~/proton-cli && go build -o ~/.hermes/bin/proton ./cmd/proton",
            exit_code=-1,
        )

    cmd = [exe, "--output", "json", "--no-input", *args]
    printable = "proton " + " ".join(args)

    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProtonError(
            f"proton-cli timed out after {timeout}s: {printable}",
            exit_code=5,
            command=printable,
        ) from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "no output"
        meaning = EXIT_MEANING.get(proc.returncode, "unknown failure")
        if proc.returncode == 2:
            detail += (
                "  |  The saved Proton session is missing or expired. "
                "The user must run `proton account login` themselves — "
                "it needs their password and 2FA and cannot be automated."
            )
        raise ProtonError(
            f"{printable} failed ({meaning}): {detail}",
            exit_code=proc.returncode,
            command=printable,
        )

    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": True, "note": "command produced no output"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Some mutations print a bare ID rather than an envelope.
        return {"ok": True, "output": out}


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------

def account() -> Any:
    return run(["account", "get"])


def profiles() -> Any:
    return run(["account", "profiles", "list"])


def session_status() -> dict:
    """Never raises. Reports whether a usable session exists."""
    if not available():
        return {"logged_in": False, "reason": "proton-cli not installed"}
    try:
        return {"logged_in": True, "account": account()}
    except ProtonError as exc:
        return {
            "logged_in": False,
            "reason": str(exc),
            "auth_required": exc.auth_required,
            "fix": "Run `proton account login` in a terminal (needs password + 2FA).",
        }


# --------------------------------------------------------------------------
# mail
# --------------------------------------------------------------------------

def mail_list(folder: str = "inbox", limit: int = 25, unread: bool = False) -> Any:
    # proton-cli v5: the page size flag is --limit (there is no --page-size).
    args = ["mail", "messages", "list", "--folder", folder, "--limit", str(limit)]
    if unread:
        args.append("--unread")
    return run(args)


def mail_search(
    keyword: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    folder: str = "all",
    after: str | None = None,
    before: str | None = None,
    limit: int = 25,
) -> Any:
    # proton-cli v5 has no `mail messages search` subcommand: every filter is a
    # flag on `mail messages list`, which doubles as the search.
    args = ["mail", "messages", "list", "--folder", folder, "--limit", str(limit)]
    for flag, value in (
        ("--keyword", keyword),
        ("--from", sender),
        ("--subject", subject),
        ("--after", after),
        ("--before", before),
    ):
        if value:
            args += [flag, value]
    return run(args)


def mail_get(ref: str, fmt: str = "text") -> Any:
    # v5 renamed --format to --render (text | html | raw).
    return run(["mail", "messages", "get", ref, "--render", fmt])


def mail_folders() -> Any:
    return run(["mail", "settings", "folders", "list"])


def mail_send(
    to: list[str],
    subject: str,
    body: str,
    *,
    confirm: bool,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attach: list[str] | None = None,
    html: bool = False,
) -> Any:
    """Send mail. Without confirm=True this runs --dry-run and sends nothing.

    The body is piped over stdin (``--body -``) rather than passed as an
    argument, so message contents never appear in the process list.
    """
    args = ["mail", "messages", "send", "--subject", subject, "--body", "-"]
    for addr in to:
        args += ["--to", addr]
    for addr in cc or []:
        args += ["--cc", addr]
    for addr in bcc or []:
        args += ["--bcc", addr]
    for path in attach or []:
        args += ["--attach", os.path.expanduser(path)]
    if html:
        args.append("--html")
    args.append("--yes" if confirm else "--dry-run")
    return run(args, timeout=300, stdin=body)


# --------------------------------------------------------------------------
# drive
# --------------------------------------------------------------------------

def drive_list(path: str = "/") -> Any:
    return run(["drive", "items", "list", path])


def drive_get(path: str) -> Any:
    return run(["drive", "items", "get", path])


def drive_download(path: str, dest_dir: str) -> Any:
    dest = os.path.expanduser(dest_dir)
    os.makedirs(dest, exist_ok=True)
    return run(["drive", "items", "download", path, "--output-dir", dest], timeout=600)


def drive_upload(local: str, remote_dir: str, *, confirm: bool) -> Any:
    src = os.path.expanduser(local)
    if not os.path.exists(src):
        raise ProtonError(f"local path does not exist: {src}", exit_code=1)
    args = ["drive", "items", "upload", src, remote_dir]
    if os.path.isdir(src):
        args.insert(3, "--recursive")
    args.append("--yes" if confirm else "--dry-run")
    return run(args, timeout=1800)


def drive_trash(path: str, *, confirm: bool) -> Any:
    args = ["drive", "items", "trash", path, "--yes" if confirm else "--dry-run"]
    return run(args)


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------

def calendars() -> Any:
    # v5 moved calendar enumeration under `calendar settings calendars list`;
    # `calendar calendars list` no longer exists.
    return run(["calendar", "settings", "calendars", "list"])


def events_list(
    start: str | None = None, end: str | None = None, calendar: str | None = None
) -> Any:
    # v5 uses --after/--before (not --start/--end) for the date range.
    args = ["calendar", "events", "list"]
    if start:
        args += ["--after", start]
    if end:
        args += ["--before", end]
    if calendar:
        args += ["--calendar", calendar]
    return run(args)


def event_get(ref: str) -> Any:
    return run(["calendar", "events", "get", ref])


def event_create(
    title: str,
    start: str,
    *,
    confirm: bool,
    duration: str | None = None,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    calendar: str | None = None,
    rrule: str | None = None,
    remind: list[str] | None = None,
) -> Any:
    args = ["calendar", "events", "create", "--title", title, "--start", start]
    if all_day:
        args.append("--all-day")
    elif duration:
        args += ["--duration", duration]
    for flag, value in (
        ("--location", location),
        ("--description", description),
        ("--calendar", calendar),
        ("--rrule", rrule),
    ):
        if value:
            args += [flag, value]
    for r in remind or []:
        args += ["--remind", r]
    args.append("--yes" if confirm else "--dry-run")
    return run(args)


def event_delete(ref: str, *, confirm: bool) -> Any:
    return run(
        ["calendar", "events", "delete", ref, "--yes" if confirm else "--dry-run"]
    )
