# Zoho People Attendance Automation – Requirements

## Goal
Automate daily check-in / check-out on Zoho People with no manual interaction.

## Functional Requirements
- Mark **Check-In** Mon–Fri at **02:00 PM PKT (Asia/Karachi)**.
- Mark **Check-Out** Mon–Fri at **11:00 PM PKT (Asia/Karachi)**.
- Skip Saturday and Sunday.
- Allow on-demand manual runs (check-in / check-out) from GitHub Actions UI.
- Run entirely in **GitHub Actions** with no self-hosted runners.

## Authentication
- Use **simple email + password login** against `accounts.zoho.com`, replicating the browser flow captured in the HAR.
- If the account has **Google Authenticator (TOTP) 2FA** enabled (which it does), the TOTP secret seed must be supplied via the `ZOHO_TOTP_SECRET` GitHub secret. The script generates the 6-digit code with `pyotp`.
- After login, scrape the per-session `conreqcsr` CSRF token from `https://people.zoho.com/<portal>/zp` and call the internal endpoint:
  - `POST https://people.zoho.com/<portal>/AttendanceAction.zp?mode=punchIn`
  - `POST https://people.zoho.com/<portal>/AttendanceAction.zp?mode=punchOut`
  - Multipart form fields: `conreqcsr`, `urlMode=myspace`, optional `latitude`/`longitude`/`accuracy`.

## GitHub Secrets / Variables
| Type | Name | Notes |
|---|---|---|
| Secret | `ZOHO_EMAIL` | required |
| Secret | `ZOHO_PASSWORD` | required |
| Secret | `ZOHO_PORTAL` | required (e.g. `rebizhr`) |
| Secret | `ZOHO_TOTP_SECRET` | required if 2FA is on |
| Variable | `ZOHO_ACCOUNTS_URL` | default `https://accounts.zoho.com` |
| Variable | `ZOHO_PEOPLE_URL` | default `https://people.zoho.com` |
| Variable | `PUNCH_TIMEZONE` | default `Asia/Karachi` |
| Variable | `PUNCH_LATITUDE` / `PUNCH_LONGITUDE` / `PUNCH_ACCURACY` | optional geolocation |

## Notifications
- After every punch run the script sends an **HTML + plain-text email** to the configured recipients via SMTP (Gmail / Google Workspace App Password by default).
- Three event types covered: `success`, `skip` (already in/out), `failure` (auth, network, or Zoho rejection). Selectable via `NOTIFY_ON`.
- Email body includes timestamp, state transition, geolocation, and the full Zoho JSON response.
- If `SMTP_USER` / `SMTP_APP_PASSWORD` are not set, notifications are silently skipped — punches still run.

## Non-functional
- Pure Python 3.11+, runtime deps: `requests`, `pyotp`, `python-dotenv`. SMTP uses the standard library only.
- CLI: `python -m src.zoho_punch --action {checkin|checkout} [--skip-weekend] [--force] [--status-only] [-v]`.
- Exit codes: `0` success or no-op, `1` auth/logic error, `2` network error.
- No HAR / `.env` / cookies committed (enforced via `.gitignore`).
