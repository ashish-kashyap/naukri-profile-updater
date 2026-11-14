# Naukri Profile Auto-Updater (Production Edition)

Automatically refreshes your Naukri.com profile daily so you stay visible at the top of recruiter search results when recruiters filter by *"Active recently"*.

---

## Features

* **Dual-action Refresh:**
  * **Resume Upload:** Automatically re-uploads your resume file to refresh the *"Resume uploaded"* timestamp.
  * **Headline Toggle:** Updates your Resume Headline (alternating between two custom variants or toggling a trailing dot) to refresh the *"Profile last updated"* timestamp.
* **Production Reliability:**
  * **Automated Retries:** 3 automatic retry attempts with exponential backoff on transient network or CDN timeouts.
  * **Anti-Bot & Stealth:** Uses native Playwright stealth techniques without breaking browser fetch metadata or prototype chains.
  * **Catch-up Scheduling:** Configured with Windows Task Scheduler `StartWhenAvailable` — if your PC was asleep or off at 9:00 AM, it runs automatically as soon as you wake it up.
  * **Battery Execution:** Configured to run on laptops even while unplugged.
  * **Windows Desktop Notifications:** Displays a native Windows notification banner upon successful update or failure.
  * **Storage Auto-cleanup:** Automatically purges screenshots and debug DOM snapshots older than 14 days.

---

## Quick Start

### 1. Install Dependencies

```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Credentials (`.env`)

Copy `.env.example` to `.env` (if not already done) and configure:

```env
NAUKRI_EMAIL=your_email@example.com
NAUKRI_PASSWORD=your_password_here

# Path to your resume PDF (optional — leave empty to only update headline)
RESUME_PATH="C:/Users/your_username/Documents/resume.pdf"
```

### 3. Test the Setup

```powershell
# Safe Dry Run: Browser opens, logs in, checks profile, but does NOT save
python update_profile.py --dry-run --visible

# Live Run with Visible Browser: Watch the full automation work
python update_profile.py --visible

# Silent / Production Run: Runs headless in the background
python update_profile.py
```

### 4. Schedule Daily Runs (Windows Task Scheduler)

```powershell
# Schedule at 09:00 AM daily (recommended)
python setup_scheduler.py

# Or choose a custom time (e.g. 08:30 AM)
python setup_scheduler.py --time 08:30

# Check scheduled status, next run time, and last result:
python setup_scheduler.py --status

# Trigger an immediate run via Task Scheduler:
python setup_scheduler.py --run-now

# Remove the scheduled task anytime:
python setup_scheduler.py --remove
```

---

## Configuration Reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `NAUKRI_EMAIL` | *(required)* | Your Naukri login email or mobile number |
| `NAUKRI_PASSWORD` | *(required)* | Your Naukri password |
| `RESUME_PATH` | *(empty)* | Full path to your resume file (`.pdf`, `.docx`) |
| `HEADLESS` | `true` | `false` to show the browser window |
| `HEADLINE_VARIANT_A` | *(auto)* | First headline variant (max 250 chars) |
| `HEADLINE_VARIANT_B` | *(auto)* | Second headline variant (max 250 chars) |
| `ACTION_DELAY_MS` | `1500` | Delay between browser actions in milliseconds |
| `MAX_RETRIES` | `3` | Maximum retry attempts per execution |
| `RETRY_DELAY_SEC` | `15` | Seconds to wait between retries (doubles each attempt) |
| `RANDOM_JITTER_MAX_SEC`| `0` | Anti-bot random wait window before starting (e.g. `60`) |
| `SEND_NOTIFICATIONS` | `true` | Windows desktop toast notifications on finish |

---

## CLI Options

```powershell
python update_profile.py [OPTIONS]

Options:
  --visible        Show the browser window (overrides HEADLESS=true)
  --dry-run        Login and inspect without saving any changes
  --skip-resume    Only update the headline (ignores RESUME_PATH)
  --skip-headline  Only upload the resume
  --retries N      Override max retry attempts (e.g. --retries 1)
  --resume-path P  Override resume file path for this run
```

---

## Logs and Diagnostics

* **Logs:** Recorded per month in `logs/naukri_YYYYMM.log`.
* **Screenshots & Snapshots:** On any warning or error, a `.png` screenshot and a `.html` DOM snapshot are saved to `screenshots/` for easy diagnosis. Files older than 14 days are automatically cleaned up.
* **State:** `.state.json` tracks which headline variant was applied last and the timestamp of the last successful run.
