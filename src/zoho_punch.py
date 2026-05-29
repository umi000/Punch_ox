"""Zoho People check-in / check-out automation.

Logs in with email + password (and optional TOTP), scrapes the per-session
``conreqcsr`` token, then POSTs to the internal AttendanceAction endpoint
exactly the same way the web UI does:

    POST https://people.zoho.com/<portal>/AttendanceAction.zp?mode=punchIn
    POST https://people.zoho.com/<portal>/AttendanceAction.zp?mode=punchOut

Multipart form fields:
    conreqcsr  - per-session CSRF token scraped from /<portal>/zp
    urlMode    - "myspace"
    latitude   - optional, sent if PUNCH_LATITUDE is set
    longitude  - optional, sent if PUNCH_LONGITUDE is set
    accuracy   - optional, defaults to 100 when coordinates are sent
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import requests

try:
    from .auth import ZohoAuthError, ZohoSession, login, request_timeout
    from .notifier import notify
except ImportError:  # Allow `python src/zoho_punch.py ...`
    from auth import ZohoAuthError, ZohoSession, login, request_timeout  # type: ignore[no-redef]
    from notifier import notify  # type: ignore[no-redef]

T = TypeVar("T")

LOGGER = logging.getLogger("zoho_punch")

DEFAULT_TIMEZONE = "Asia/Karachi"
DEFAULT_ACCURACY = "100"


class PunchError(RuntimeError):
    """Raised when the attendance endpoint returns an error."""


def _build_punch_form(zs: ZohoSession) -> dict[str, str]:
    fields: dict[str, str] = {
        "conreqcsr": zs.conreqcsr,
        "urlMode": "myspace",
    }

    lat = os.environ.get("PUNCH_LATITUDE", "").strip()
    lng = os.environ.get("PUNCH_LONGITUDE", "").strip()
    if lat and lng:
        fields["latitude"] = lat
        fields["longitude"] = lng
        fields["accuracy"] = os.environ.get("PUNCH_ACCURACY", DEFAULT_ACCURACY)
    return fields


def _now_in_zone(tz_name: str) -> datetime:
    try:
        return datetime.now(tz=ZoneInfo(tz_name))
    except Exception as exc:
        raise PunchError(f"Invalid PUNCH_TIMEZONE '{tz_name}': {exc}") from exc


def _is_weekday(tz_name: str) -> bool:
    try:
        return _now_in_zone(tz_name).weekday() < 5
    except PunchError:
        return True


def _retry_on_network(
    label: str,
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 5.0,
) -> T:
    """Retry transient read/connect timeouts (common from GitHub Actions)."""
    last_exc: requests.RequestException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = base_delay * attempt
            LOGGER.warning(
                "%s failed (attempt %s/%s): %s — retrying in %.0fs",
                label,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def get_status(zs: ZohoSession, *, timeout: int | None = None) -> dict[str, Any]:
    """Return Zoho's current attendance state for the logged-in user."""
    if timeout is None:
        timeout = request_timeout()
    url = f"{zs.people_url}/{zs.portal}/AttendanceAction.zp"
    headers = {
        "Origin": zs.people_url,
        "Referer": zs.landing_url(),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    body = f"mode=getStatus&conreqcsr={zs.conreqcsr}"
    LOGGER.debug("POST %s mode=getStatus", url)

    def _post() -> dict[str, Any]:
        resp = zs.session.post(url, headers=headers, data=body, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:500]}
        if resp.status_code != 200 or not isinstance(payload, dict):
            raise PunchError(f"getStatus HTTP {resp.status_code}: {payload}")
        return payload

    return _retry_on_network("getStatus", _post)


def is_checked_in(status: dict[str, Any]) -> bool:
    """True when Zoho considers the user currently checked in (Office In)."""
    label = str(
        status.get("leaveAttStatUnEncoded")
        or status.get("leaveAttStat")
        or ""
    ).strip().lower()
    if label in {"office in", "in"}:
        return True
    if label in {"office out", "out"}:
        return False
    if "allowedToCheckIn" in status:
        return not bool(status["allowedToCheckIn"])
    response = status.get("response")
    if isinstance(response, list) and response:
        head = response[0]
        if isinstance(head, dict) and head.get("allowPunchOut") is True:
            return True
    return False


def state_label(status: dict[str, Any]) -> str:
    raw = str(
        status.get("leaveAttStatUnEncoded")
        or status.get("leaveAttStat")
        or ""
    ).strip()
    return raw or ("Office In" if is_checked_in(status) else "Office Out")


def punch(action: str, zs: ZohoSession, *, timeout: int | None = None) -> dict[str, Any]:
    if timeout is None:
        timeout = request_timeout()
    if action not in {"checkin", "checkout"}:
        raise PunchError(f"Unknown action '{action}'")

    mode = "punchIn" if action == "checkin" else "punchOut"
    url = zs.attendance_url(mode)
    form = _build_punch_form(zs)

    headers = {
        "Origin": zs.people_url,
        "Referer": zs.landing_url(),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
    }

    LOGGER.info("Submitting %s -> %s", action, url)
    LOGGER.debug("Form fields: %s", {k: ("<set>" if k == "conreqcsr" else v) for k, v in form.items()})

    multipart_files = {k: (None, v) for k, v in form.items()}

    def _post() -> dict[str, Any]:
        resp = zs.session.post(url, headers=headers, files=multipart_files, timeout=timeout)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}

        if resp.status_code != 200:
            raise PunchError(f"Attendance HTTP {resp.status_code}: {body}")

        if isinstance(body, dict):
            if body.get("error") or body.get("errorCode"):
                raise PunchError(_friendly_error(body))
            if body.get("fail"):
                raise PunchError(_friendly_error(body))
            inner = body.get("punchIn") if action == "checkin" else body.get("punchOut")
            if isinstance(inner, dict) and inner.get("error"):
                raise PunchError(_friendly_error(body))
            if body.get("msg") and isinstance(body["msg"], str) and body["msg"].lower().startswith("error"):
                raise PunchError(f"Attendance API error message: {body}")

        return body

    return _retry_on_network(f"punch {action}", _post)


_ERROR_MESSAGES = {
    "punchin_error5": "Already checked in - cannot check in again until you check out.",
    "punchout_error5": "Already checked out - cannot check out again until you check in.",
    "alreadyin": "You are already checked in.",
    "alreadyout": "You are already checked out.",
    "punchin_error1": "Punch was rejected (error1) - typically location / shift validation.",
    "punchout_error1": "Punch was rejected (error1) - typically location / shift validation.",
    "punchin_error2": "Punch was rejected (error2).",
    "punchout_error2": "Punch was rejected (error2).",
}


def _friendly_error(body: dict[str, Any]) -> str:
    candidates: list[Any] = [body.get("fail"), body.get("error")]
    for key in ("punchIn", "punchOut"):
        nested = body.get(key)
        if isinstance(nested, dict):
            candidates.append(nested.get("error"))
            candidates.append(nested.get("fail"))

    raw = next((str(c).strip() for c in candidates if c), "")
    friendly = _ERROR_MESSAGES.get(raw.lower())
    if friendly:
        return f"{friendly} (raw response: {body})"
    return f"Attendance API returned error: {body}"


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zoho People check-in / check-out")
    parser.add_argument(
        "--action",
        required=True,
        choices=["checkin", "checkout"],
        help="Which action to perform.",
    )
    parser.add_argument(
        "--skip-weekend",
        action="store_true",
        help="Exit successfully (no punch) when run on Saturday/Sunday.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the pre-flight getStatus check and submit the punch unconditionally.",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Just print the current Office In/Out state and exit; no punch submitted.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    tz_name = os.environ.get("PUNCH_TIMEZONE", DEFAULT_TIMEZONE)

    if args.skip_weekend and not _is_weekday(tz_name):
        now_local = _now_in_zone(tz_name)
        LOGGER.info("Today is a weekend in %s - skipping %s.", tz_name, args.action)
        notify(
            event="skip",
            action=args.action,
            summary=f"Weekend in {tz_name} — no punch attempted",
            detail_lines=[
                ("Local date", now_local.strftime("%A, %Y-%m-%d")),
                ("Reason", f"--skip-weekend: {args.action} not run on Sat/Sun"),
            ],
            payload={"timezone": tz_name, "local_iso": now_local.isoformat()},
        )
        return 0

    try:
        zs, status = _authenticate_and_status()

        if args.status_only:
            print(json.dumps({"state": state_label(status), "raw": status}, indent=2, default=str))
            return 0

        return _run_action(args.action, zs, status, force=args.force)

    except ZohoAuthError as exc:
        LOGGER.error("Authentication failed: %s", exc)
        notify(
            event="failure",
            action=args.action,
            summary="Authentication failed",
            detail_lines=[("Error", str(exc))],
            payload={"error": str(exc)},
        )
        return 1
    except requests.RequestException as exc:
        LOGGER.error("Network error: %s", exc)
        notify(
            event="failure",
            action=args.action,
            summary="Network error",
            detail_lines=[("Error", str(exc))],
            payload={"error": str(exc)},
        )
        return 2


def _authenticate_and_status() -> tuple[ZohoSession, dict[str, Any]]:
    LOGGER.info("Authenticating with Zoho ...")
    zs = _retry_on_network("login", login)
    LOGGER.info("Authenticated.")
    status = get_status(zs)
    LOGGER.info("Current Zoho attendance state: %s", state_label(status))
    return zs, status


def _run_action(action: str, zs: ZohoSession, status: dict[str, Any], *, force: bool) -> int:
    currently_in = is_checked_in(status)
    label = state_label(status)
    action_label = "Check-In" if action == "checkin" else "Check-Out"

    if not force:
        if action == "checkin" and currently_in:
            LOGGER.info("Already checked in - nothing to do. Exiting cleanly.")
            notify(
                event="skip",
                action=action,
                summary=f"Already {label}",
                detail_lines=[("State", label), ("Reason", "No-op: already checked in")],
                payload=status,
            )
            return 0
        if action == "checkout" and not currently_in:
            LOGGER.info("Already checked out - nothing to do. Exiting cleanly.")
            notify(
                event="skip",
                action=action,
                summary=f"Already {label}",
                detail_lines=[("State", label), ("Reason", "No-op: already checked out")],
                payload=status,
            )
            return 0

    try:
        LOGGER.info("Submitting %s ...", action)
        body = punch(action, zs)
    except PunchError as exc:
        LOGGER.error("Punch failed: %s", exc)
        notify(
            event="failure",
            action=action,
            summary=f"{action_label} rejected by Zoho",
            detail_lines=[("State before", label), ("Error", str(exc))],
            payload={"error": str(exc)},
        )
        return 1

    LOGGER.info("%s succeeded.", action_label)
    print(json.dumps(body, indent=2, sort_keys=True, default=str))
    notify(
        event="success",
        action=action,
        summary=f"{action_label} recorded successfully",
        detail_lines=[("Previous state", label), ("New state", "Office In" if action == "checkin" else "Office Out")],
        payload=body,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
