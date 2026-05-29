"""Email + password (and optional TOTP) login for Zoho People.

Mimics the browser flow captured in the HAR:

    1. GET  https://accounts.zoho.com/signin?servicename=zohopeople
       -> obtain `iamcsrcoo` cookie / X-ZCSRF-TOKEN value
    2. POST https://accounts.zoho.com/signin/v2/lookup/<email>
       -> returns { identifier: <userId>, ... } and may already authorise
    3. POST https://accounts.zoho.com/signin/v2/primary/<userId>/password
       -> body: {"passwordauth": {"password": "..."}}
       -> may return next-step (TOTP) or final SUCCESS
    4. (If required) POST .../v2/secondary/<userId>/totp/verify
       -> body: {"totpsecondauth": {"otp": "<6-digit code>", ...}}
    5. GET  https://people.zoho.com/<portal>/zp
       -> session cookies are now valid for People; HTML embeds the
          per-session `conreqcsr` CSRF token used by AttendanceAction.zp.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 60


def request_timeout() -> int:
    """HTTP read/connect timeout in seconds (env: ZOHO_REQUEST_TIMEOUT)."""
    raw = os.environ.get("ZOHO_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ZohoAuthError(f"ZOHO_REQUEST_TIMEOUT must be an integer, got '{raw}'") from exc
    if value < 10:
        raise ZohoAuthError("ZOHO_REQUEST_TIMEOUT must be at least 10 seconds.")
    return value


DEFAULT_ACCOUNTS_URL = "https://accounts.zoho.com"
DEFAULT_PEOPLE_URL = "https://people.zoho.com"
SERVICE_NAME = "zohopeople"
SERVICE_URL = "https://people.zoho.com/people"
SIGNUP_URL = "https://www.zoho.com/people/signup.html?servicename=zohopeople"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

CSRF_TOKEN_RE = re.compile(
    r"""(?:var\s+csrfToken|["']csrfToken["']|conreqcsr)\s*[:=]\s*["']([0-9a-f]{32,})["']""",
    re.IGNORECASE,
)


class ZohoAuthError(RuntimeError):
    """Raised when authentication fails."""


@dataclass
class ZohoSession:
    """Holds an authenticated requests.Session and derived metadata."""

    session: requests.Session
    accounts_url: str
    people_url: str
    portal: str
    conreqcsr: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def attendance_url(self, mode: str) -> str:
        return f"{self.people_url}/{self.portal}/AttendanceAction.zp?mode={mode}"

    def landing_url(self) -> str:
        return f"{self.people_url}/{self.portal}/zp"


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
        }
    )
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=2,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# Zoho has changed the CSRF cookie name over time. We try the known variants
# in order of likelihood.
_CSRF_COOKIE_CANDIDATES = ("iamcsr", "iamcsrcoo", "iamtfacsr")


def _load_signin_page(session: requests.Session, accounts_url: str, *, timeout: int) -> str:
    url = (
        f"{accounts_url}/signin"
        f"?servicename={SERVICE_NAME}&signupurl={SIGNUP_URL}"
    )
    LOGGER.debug("GET %s", url)
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()

    for name in _CSRF_COOKIE_CANDIDATES:
        value = session.cookies.get(name)
        if value:
            LOGGER.debug("Using CSRF cookie %s", name)
            return value

    raise ZohoAuthError(
        "Failed to obtain CSRF cookie from sign-in page; "
        f"received cookies: {list(session.cookies.keys())}"
    )


def _csrf_header(token: str) -> dict[str, str]:
    return {"X-ZCSRF-TOKEN": f"iamcsrcoo={token}"}


def _lookup(
    session: requests.Session, accounts_url: str, csrf: str, email: str, *, timeout: int
) -> dict[str, Any]:
    url = f"{accounts_url}/signin/v2/lookup/{requests.utils.quote(email, safe='')}"
    body = {
        "mode": "primary",
        "cli_time": str(int(time.time() * 1000)),
        "servicename": SERVICE_NAME,
        "signupurl": SIGNUP_URL,
        "serviceurl": SERVICE_URL,
    }
    headers = {
        "Origin": accounts_url,
        "Referer": f"{accounts_url}/signin?servicename={SERVICE_NAME}",
        "X-Requested-With": "XMLHttpRequest",
        **_csrf_header(csrf),
    }
    LOGGER.debug("POST %s", url)
    resp = session.post(url, data=body, headers=headers, timeout=timeout)
    payload = _safe_json(resp)
    if resp.status_code != 200 or not isinstance(payload, dict):
        raise ZohoAuthError(f"Lookup failed (HTTP {resp.status_code}): {payload}")

    status_code = str(payload.get("status_code", ""))
    if status_code and status_code not in {"200", "201"}:
        raise ZohoAuthError(f"Lookup rejected: {payload}")
    return payload


def _password_auth(
    session: requests.Session,
    accounts_url: str,
    csrf: str,
    user_id: str,
    digest: str,
    password: str,
    *,
    timeout: int,
) -> dict[str, Any]:
    qs = {
        "digest": digest,
        "cli_time": str(int(time.time() * 1000)),
        "servicename": SERVICE_NAME,
        "signupurl": SIGNUP_URL,
        "serviceurl": SERVICE_URL,
    }
    url = f"{accounts_url}/signin/v2/primary/{user_id}/password"
    headers = {
        "Origin": accounts_url,
        "Referer": f"{accounts_url}/signin?servicename={SERVICE_NAME}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        **_csrf_header(csrf),
    }
    body = '{"passwordauth":{"password":"' + password.replace("\\", "\\\\").replace('"', '\\"') + '"}}'
    LOGGER.debug("POST %s", url)
    resp = session.post(url, params=qs, data=body, headers=headers, timeout=timeout)
    payload = _safe_json(resp)
    if resp.status_code != 200:
        raise ZohoAuthError(f"Password auth HTTP {resp.status_code}: {payload}")
    return payload


def _totp_verify(
    session: requests.Session,
    accounts_url: str,
    csrf: str,
    user_id: str,
    otp: str,
    *,
    timeout: int,
) -> dict[str, Any]:
    url = f"{accounts_url}/signin/v2/secondary/{user_id}/totp/verify"
    qs = {
        "cli_time": str(int(time.time() * 1000)),
        "servicename": SERVICE_NAME,
        "serviceurl": SERVICE_URL,
        "remembertfa": "true",
    }
    body = '{"totpsecondauth":{"otp":"' + otp + '","remembertfa":true}}'
    headers = {
        "Origin": accounts_url,
        "Referer": f"{accounts_url}/signin?servicename={SERVICE_NAME}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        **_csrf_header(csrf),
    }
    LOGGER.debug("POST %s", url)
    resp = session.post(url, params=qs, data=body, headers=headers, timeout=timeout)
    payload = _safe_json(resp)
    if resp.status_code != 200:
        raise ZohoAuthError(f"TOTP verify HTTP {resp.status_code}: {payload}")
    return payload


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:500]}


def _looks_like_tfa(payload: dict[str, Any]) -> bool:
    """Detect a real 2FA challenge (not a TFA-recommendation banner)."""
    code = str(payload.get("code", "")).upper()
    if code.startswith("MFA") or code.startswith("TFA"):
        return True

    next_step = (
        payload.get("next")
        or payload.get("redirect_uri")
        or payload.get("location")
        or ""
    )
    inner = payload.get("password") or payload.get("passwordauth") or {}
    if isinstance(inner, dict):
        next_step = next_step or inner.get("next") or inner.get("redirect_uri") or ""

    next_lower = str(next_step).lower()
    challenge_markers = (
        "/v2/secondary/",
        "/secondary/",
        "/totp/",
        "/oneauth/",
        "/mfa/",
        "twofactor",
        "two_factor",
    )
    return any(marker in next_lower for marker in challenge_markers)


def _generate_totp(secret: str) -> str:
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover
        raise ZohoAuthError(
            "TOTP code required but `pyotp` is not installed. "
            "Add `pyotp` to requirements.txt."
        ) from exc

    code = pyotp.TOTP(secret.replace(" ", "")).now()
    LOGGER.debug("Generated TOTP code")
    return code


def _establish_people_session(
    session: requests.Session,
    people_url: str,
    portal: str,
    *,
    timeout: int,
) -> requests.Response:
    """Complete SSO, then load the portal home page (required for attendance APIs)."""
    service_url = f"{people_url}/people"
    LOGGER.debug("GET %s (SSO redirect chain)", service_url)
    sso_resp = session.get(service_url, timeout=timeout, allow_redirects=True)
    sso_resp.raise_for_status()

    final = sso_resp.url.lower()
    if "signin" in final or "/announcement/" in final or "accounts.zoho.com" in final:
        LOGGER.warning("SSO did not land on People (final URL=%s)", sso_resp.url)

    portal_url = f"{people_url}/{portal}/zp"
    LOGGER.debug("GET %s (portal session + conreqcsr)", portal_url)
    portal_resp = session.get(portal_url, timeout=timeout, allow_redirects=True)
    portal_resp.raise_for_status()

    portal_final = portal_resp.url.lower()
    if "accounts.zoho.com" in portal_final or "signin" in portal_final:
        raise ZohoAuthError(
            f"Portal page redirected to login (final URL={portal_resp.url}). "
            "Check ZOHO_PORTAL and that this account can access the portal."
        )
    return portal_resp


def _extract_csrf_token(session: requests.Session, html: str) -> str:
    """Pull the per-session CSRF token from either the HTML or the cookie jar."""
    for cookie_name in ("CSRF_TOKEN", "_zpsid_csrf", "csrftoken"):
        value = session.cookies.get(cookie_name, default=None, domain="people.zoho.com")
        if value:
            LOGGER.debug("Using CSRF token from cookie %s", cookie_name)
            return value

    match = CSRF_TOKEN_RE.search(html)
    if match:
        LOGGER.debug("Using CSRF token scraped from landing HTML")
        return match.group(1)

    raise ZohoAuthError(
        "Could not locate the CSRF token in either cookies or landing HTML. "
        "Login likely succeeded but session is not authorised for the portal."
    )


def login() -> ZohoSession:
    """Perform the full email + password (+ TOTP if required) login.

    Returns a :class:`ZohoSession` ready to call attendance endpoints.
    """

    email = os.environ.get("ZOHO_EMAIL", "").strip()
    password = os.environ.get("ZOHO_PASSWORD", "").strip()
    if not email or not password:
        raise ZohoAuthError("ZOHO_EMAIL and ZOHO_PASSWORD must be set.")

    accounts_url = os.environ.get("ZOHO_ACCOUNTS_URL", DEFAULT_ACCOUNTS_URL).rstrip("/")
    people_url = os.environ.get("ZOHO_PEOPLE_URL", DEFAULT_PEOPLE_URL).rstrip("/")
    portal = os.environ.get("ZOHO_PORTAL", "").strip()
    if not portal:
        raise ZohoAuthError(
            "ZOHO_PORTAL must be set (e.g. 'rebizhr' from "
            "https://people.zoho.com/<portal>/zp)."
        )

    session = _build_session()
    timeout = request_timeout()
    csrf = _load_signin_page(session, accounts_url, timeout=timeout)

    lookup_envelope = _lookup(session, accounts_url, csrf, email, timeout=timeout)
    lookup_inner = lookup_envelope.get("lookup", lookup_envelope)
    user_id = str(
        lookup_inner.get("identifier")
        or lookup_inner.get("USER_ID")
        or lookup_envelope.get("identifier")
        or ""
    ).strip()
    digest = str(
        lookup_inner.get("digest")
        or lookup_envelope.get("digest")
        or ""
    ).strip()
    if not user_id or not digest:
        raise ZohoAuthError(f"Lookup response missing identifier/digest: {lookup_envelope}")
    LOGGER.debug("Lookup OK userId=%s", user_id)

    pw_resp = _password_auth(
        session, accounts_url, csrf, user_id, digest, password, timeout=timeout
    )
    status_code = str(pw_resp.get("status_code", ""))
    code = str(pw_resp.get("code", ""))
    pw_inner = pw_resp.get("password") or pw_resp.get("passwordauth") or {}
    next_step = (
        pw_resp.get("next")
        or pw_resp.get("redirect_uri")
        or pw_inner.get("next")
        or pw_inner.get("redirect_uri")
        or ""
    )
    LOGGER.debug(
        "Password response status_code=%s code=%s next=%s",
        status_code,
        code,
        next_step,
    )

    if (
        status_code in {"E401", "401", "400"}
        or code.startswith("E")
        or pw_resp.get("status") == "failure"
    ) and not _looks_like_tfa(pw_resp):
        raise ZohoAuthError(f"Login failed (wrong password?): {pw_resp}")

    needs_totp = _looks_like_tfa(pw_resp)

    if needs_totp:
        totp_secret = os.environ.get("ZOHO_TOTP_SECRET", "").strip()
        if not totp_secret:
            raise ZohoAuthError(
                "Zoho is asking for Google Authenticator (TOTP) but ZOHO_TOTP_SECRET "
                "is not set. Add the TOTP seed (the QR-code secret you saved when "
                "enabling 2FA) as a GitHub Actions secret to enable automated login."
            )
        otp = _generate_totp(totp_secret)
        totp_resp = _totp_verify(session, accounts_url, csrf, user_id, otp, timeout=timeout)
        if str(totp_resp.get("status_code", "")) in {"E401", "401"}:
            raise ZohoAuthError(f"TOTP verify failed: {totp_resp}")

    landing_resp = _establish_people_session(
        session, people_url, portal, timeout=timeout
    )
    conreqcsr = _extract_csrf_token(session, landing_resp.text)

    return ZohoSession(
        session=session,
        accounts_url=accounts_url,
        people_url=people_url,
        portal=portal,
        conreqcsr=conreqcsr,
        extras={"user_id": user_id, "lookup": lookup_envelope},
    )
