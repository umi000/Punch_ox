# Zoho People Attendance Automation

Automated daily check-in / check-out on Zoho People, designed to run unattended in **GitHub Actions**.

- **Check-In:** every weekday (Mon–Fri) at **~11:05 AM PKT** (06:05 UTC)
- **Check-Out:** every weekday (Mon–Fri) at **~8:05 PM PKT** (15:05 UTC; ~9 h after check-in)
- Logs in with **email + password** (and TOTP if 2FA is enabled), then calls the same internal endpoint the web UI uses.

---

## How it works

1. The script logs in to `accounts.zoho.com` with your email + password (and a generated TOTP code if 2FA is on).
2. It then loads `https://people.zoho.com/<portal>/zp` to scrape the per-session `conreqcsr` CSRF token.
3. It POSTs to `…/AttendanceAction.zp?mode=punchIn` (or `punchOut`) — exactly the request the web UI makes when you click the button.

No browser, no Selenium, no Playwright.

---

## ⚠️ Important: 2FA

Your Zoho account has **Google Authenticator (TOTP) 2FA** enabled. In your local browser, Zoho remembers the device and skips the OTP. **GitHub Actions runs from a different IP every time**, so 2FA *will* be challenged.

You have two choices:

| Option | What to do |
|---|---|
| **A) Provide TOTP secret (recommended)** | Disable 2FA temporarily, re-enable it, and during setup choose *“Can’t scan the QR code?”* — Zoho will reveal the base32 secret string. Save that as the `ZOHO_TOTP_SECRET` GitHub secret. The script will generate a fresh 6-digit code automatically. |
| **B) Disable 2FA on this account** | Removes the need for `ZOHO_TOTP_SECRET`. Less secure. |

---

## One-time setup

### 1. Add GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value | Required |
|---|---|---|
| `ZOHO_EMAIL` | your Zoho login email | ✅ |
| `ZOHO_PASSWORD` | your Zoho password | ✅ |
| `ZOHO_PORTAL` | your portal slug (`rebizhr` for you) | ✅ |
| `ZOHO_TOTP_SECRET` | base32 TOTP seed | ✅ if 2FA enabled |

### 2. Add GitHub Actions variables (optional)

Repo → **Settings → Secrets and variables → Actions → Variables tab**:

| Variable | Default | When to change |
|---|---|---|
| `ZOHO_ACCOUNTS_URL` | `https://accounts.zoho.com` | non-US data center |
| `ZOHO_PEOPLE_URL`   | `https://people.zoho.com`   | non-US data center |
| `PUNCH_TIMEZONE`    | `Asia/Karachi`              | different country |
| `PUNCH_LATITUDE`    | *(unset)*                   | want geo on punches (e.g. `25.395759`) |
| `PUNCH_LONGITUDE`   | *(unset)*                   | want geo on punches (e.g. `68.362558`) |
| `PUNCH_ACCURACY`    | `100`                       | rarely needed |

> If you set lat/lng, set both. They’re sent the same way the web UI does.

### 3. Email notifications (optional)

The script can email you after every punch (success / skip / failure). Works with any SMTP server; the example below uses **Gmail App Password**.

#### Generate a Gmail App Password
1. Go to <https://myaccount.google.com/apppasswords> (sign in if prompted).
2. App name: `Punch_ox` (or anything you like) → **Create**.
3. Copy the 16-character password (no spaces).

#### Add the secrets / variables

| Type | Name | Value |
|---|---|---|
| Secret | `SMTP_USER` | the Gmail address that owns the App Password |
| Secret | `SMTP_APP_PASSWORD` | 16-char App Password |
| Variable | `NOTIFY_TO` | comma-separated recipient list (defaults to `SMTP_USER`) |
| Variable | `NOTIFY_FROM` | display name in the From header (default: `Zoho Punch Bot`) |
| Variable | `NOTIFY_ON` | which events trigger an email (default: `success,skip,failure`) |
| Variable | `SMTP_HOST` | non-Gmail SMTP server (default: `smtp.gmail.com`) |
| Variable | `SMTP_PORT` | non-default port (default: `587` for STARTTLS, `465` for SSL) |

> If `SMTP_USER` or `SMTP_APP_PASSWORD` is missing, email is **silently disabled** — your punches still run.

#### What the email looks like

Subjects:
- `[Zoho Punch] SUCCESS - Check-In - Check-In recorded successfully`
- `[Zoho Punch] SKIPPED - Check-In - Already Office In`
- `[Zoho Punch] FAILURE - Check-Out - Check-Out rejected by Zoho`

Body (HTML version): coloured banner (green / gray / red), key/value table with timestamp + state transition + location, and the **full Zoho JSON response** in a code block. A plain-text fallback is included for clients that don't render HTML.

---

## Schedule

Defined in [`.github/workflows/zoho-attendance.yml`](.github/workflows/zoho-attendance.yml):

| Action | Local (PKT) | UTC cron | Days |
|---|---|---|---|
| Check-In  | 11:05 | `05 6 * * 1-5`  | Mon–Fri |
| Check-Out | 20:05 | `05 15 * * 1-5` | Mon–Fri |

> ⚠️ GitHub Actions cron is **best-effort** — it can be delayed by 5–15 minutes during peak load. The script also self-skips on Saturday/Sunday in case of timezone-boundary edge cases.

You can also trigger a punch manually from the **Actions** tab → *Zoho People Attendance* → **Run workflow** → pick `checkin` or `checkout`.

---

## Local testing

```powershell
# In the project root
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Copy template and fill in real values
Copy-Item config.example.env .env
notepad .env

# Load the .env into the current PowerShell session
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim())
  }
}

# Run
python -m src.zoho_punch --action checkin --verbose
python -m src.zoho_punch --action checkout --verbose
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Lookup rejected` | Wrong email or wrong data center URL. |
| `Login failed (wrong password?)` | Wrong password. |
| `Zoho is asking for Google Authenticator (TOTP) but ZOHO_TOTP_SECRET is not set` | 2FA triggered; add the TOTP seed secret. |
| `Could not find conreqcsr token` | Login succeeded but Zoho redirected somewhere unexpected (often a captcha or "trust this device" page). Try logging in once via the browser, then retry; or switch to a fresh password. |
| `Attendance HTTP 401 / INVALID_TICKET` | Session lost between login and punch — re-run; if it persists check that `ZOHO_PORTAL` is correct. |

---

## Security

- **Your password lives in GitHub Secrets**, encrypted at rest. Use a strong, unique password.
- The HAR file (`people.zoho.com.har`) is git-ignored because it contains the password in plaintext — **never commit it**. It’s recommended to **change your Zoho password** since the HAR file already exposed it.
- The TOTP secret is also a GitHub secret. Treat it like a password.

---

## Files

```
.
├── .github/workflows/zoho-attendance.yml   # CI schedule + manual trigger
├── src/
│   ├── auth.py        # email+password+TOTP login flow
│   ├── notifier.py    # SMTP email notifier (HTML + plain text)
│   └── zoho_punch.py  # main entry point
├── config.example.env # template for local .env
├── requirements.txt
└── README.md
```
