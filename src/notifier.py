"""Email notification helper for Zoho People punch events.

Designed for Gmail / Google Workspace (App Password), but works with any
SMTP server that supports STARTTLS.

Configuration (all optional - if SMTP_USER or SMTP_APP_PASSWORD is missing,
notifications are silently skipped):

    SMTP_HOST            - default: smtp.gmail.com
    SMTP_PORT            - default: 587 (STARTTLS); use 465 for SSL
    SMTP_USER            - the Gmail address that owns the App Password
    SMTP_APP_PASSWORD    - 16-character Google App Password
    NOTIFY_FROM          - optional display name, e.g. "Zoho Punch Bot"
    NOTIFY_TO            - comma-separated recipient list; defaults to SMTP_USER
    NOTIFY_ON            - comma-separated subset of {success, skip, failure};
                           default: success,skip,failure
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("notifier")

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587
DEFAULT_TIMEZONE = "Asia/Karachi"

# Visual accents for the HTML email banner
EVENT_COLOURS = {
    "success": "#22c55e",  # green
    "skip": "#6b7280",     # gray
    "failure": "#ef4444",  # red
}

ACTION_LABELS = {
    "checkin": "Check-In",
    "checkout": "Check-Out",
}

EVENT_LABELS = {
    "success": "SUCCESS",
    "skip": "SKIPPED",
    "failure": "FAILURE",
}


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender_name: str
    recipients: tuple[str, ...]
    enabled_events: frozenset[str]

    @classmethod
    def from_env(cls) -> "SmtpConfig | None":
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_APP_PASSWORD", "").strip()
        if not user or not password:
            LOGGER.debug("SMTP_USER / SMTP_APP_PASSWORD not set - notifications disabled.")
            return None

        recipients_raw = os.environ.get("NOTIFY_TO", "").strip() or user
        recipients = tuple(r.strip() for r in recipients_raw.split(",") if r.strip())

        enabled_raw = os.environ.get("NOTIFY_ON", "success,skip,failure")
        enabled = frozenset(e.strip().lower() for e in enabled_raw.split(",") if e.strip())

        return cls(
            host=os.environ.get("SMTP_HOST", DEFAULT_HOST),
            port=int(os.environ.get("SMTP_PORT", DEFAULT_PORT)),
            user=user,
            password=password,
            sender_name=os.environ.get("NOTIFY_FROM", "Zoho Punch Bot"),
            recipients=recipients,
            enabled_events=enabled,
        )


def _now_pkt() -> str:
    tz_name = os.environ.get("PUNCH_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        return datetime.now(tz=ZoneInfo(tz_name)).strftime("%a, %d %b %Y - %I:%M:%S %p %Z")
    except Exception:
        return datetime.utcnow().strftime("%a, %d %b %Y - %H:%M:%S UTC")


def _build_subject(event: str, action: str, summary: str) -> str:
    label = EVENT_LABELS.get(event, event.upper())
    action_label = ACTION_LABELS.get(action, action.title())
    if summary:
        return f"[Zoho Punch] {label} - {action_label} - {summary}"
    return f"[Zoho Punch] {label} - {action_label}"


def _build_plain_body(
    *,
    event: str,
    action: str,
    summary: str,
    detail_lines: list[tuple[str, str]],
    payload: Any,
) -> str:
    label = EVENT_LABELS.get(event, event.upper())
    action_label = ACTION_LABELS.get(action, action.title())
    width = 60

    lines = [
        "=" * width,
        f"  Zoho People Attendance - {label}",
        f"  {action_label}",
        "=" * width,
        "",
    ]
    if summary:
        lines.append(f"Summary : {summary}")
    for key, value in detail_lines:
        lines.append(f"{key:<8}: {value}")

    lines.extend([
        "",
        "-" * width,
        "Zoho API Response",
        "-" * width,
        json.dumps(payload, indent=2, default=str, sort_keys=True) if payload is not None else "(no payload)",
        "",
        "-- Sent automatically by Punch_ox (https://github.com/umi000/Punch_ox)",
    ])
    return "\n".join(lines)


def _build_html_body(
    *,
    event: str,
    action: str,
    summary: str,
    detail_lines: list[tuple[str, str]],
    payload: Any,
) -> str:
    colour = EVENT_COLOURS.get(event, "#374151")
    label = EVENT_LABELS.get(event, event.upper())
    action_label = ACTION_LABELS.get(action, action.title())

    rows = []
    if summary:
        rows.append(("Summary", summary))
    rows.extend(detail_lines)

    table_html = "".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-weight:600;width:120px;'>{escape(k)}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e5e7eb;color:#111827;'>{escape(v)}</td>"
        f"</tr>"
        for k, v in rows
    )

    payload_text = (
        json.dumps(payload, indent=2, default=str, sort_keys=True)
        if payload is not None
        else "(no payload)"
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#f3f4f6;padding:24px;margin:0;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.08);overflow:hidden;">
    <div style="background:{colour};padding:18px 24px;color:#ffffff;">
      <div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;opacity:0.85;">Zoho People Attendance</div>
      <div style="font-size:22px;font-weight:700;margin-top:4px;">{escape(label)} - {escape(action_label)}</div>
    </div>
    <div style="padding:20px 24px;">
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        {table_html}
      </table>
      <div style="margin-top:24px;font-size:12px;color:#6b7280;font-weight:600;letter-spacing:0.5px;text-transform:uppercase;">Zoho API Response</div>
      <pre style="background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:6px;overflow:auto;font-size:12px;line-height:1.5;margin-top:8px;font-family:'SF Mono',Menlo,Consolas,monospace;">{escape(payload_text)}</pre>
    </div>
    <div style="padding:12px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;text-align:center;">
      Sent automatically by Punch_ox - <a href="https://github.com/umi000/Punch_ox" style="color:#9ca3af;">github.com/umi000/Punch_ox</a>
    </div>
  </div>
</body>
</html>"""


def notify(
    *,
    event: str,
    action: str,
    summary: str = "",
    detail_lines: list[tuple[str, str]] | None = None,
    payload: Any = None,
    config: SmtpConfig | None = None,
) -> bool:
    """Send a notification email. Returns True if delivered, False otherwise.

    The function never raises - SMTP failures are logged and swallowed so
    that they cannot mask the original punch outcome.
    """
    cfg = config or SmtpConfig.from_env()
    if cfg is None:
        return False
    if event not in cfg.enabled_events:
        LOGGER.debug("Event '%s' not in NOTIFY_ON=%s - skipping email.", event, sorted(cfg.enabled_events))
        return False

    detail_lines = list(detail_lines or [])
    detail_lines.insert(0, ("When", _now_pkt()))

    msg = EmailMessage()
    msg["Subject"] = _build_subject(event, action, summary)
    msg["From"] = formataddr((cfg.sender_name, cfg.user))
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.host)

    msg.set_content(_build_plain_body(
        event=event, action=action, summary=summary,
        detail_lines=detail_lines, payload=payload,
    ))
    msg.add_alternative(_build_html_body(
        event=event, action=action, summary=summary,
        detail_lines=detail_lines, payload=payload,
    ), subtype="html")

    try:
        if cfg.port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=30) as smtp:
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
    except Exception as exc:
        LOGGER.error("Failed to send notification email: %s", exc)
        return False

    LOGGER.info("Notification email sent to %s", ", ".join(cfg.recipients))
    return True
