# Naukri Profile Auto-Updater (Production Edition)

Automatically refreshes your Naukri.com profile daily so you stay visible at the top of recruiter search results when recruiters filter candidates by *"Active recently"*.

Supports **two deployment modes**:
1. **100% Cloud (GitHub Actions):** Runs in the cloud every morning — **no PC/laptop needed, zero hosting cost**, and can be triggered on demand from your phone.
2. **Local (Windows Task Scheduler):** Runs silently on your local Windows PC with battery support and missed-run catchup.

---

## 🚀 3-Minute Cloud Setup (No Laptop Needed / Free for Any User)

Anyone can run this automated daily in the cloud for free without keeping any computer on:

### Step 1: Fork this Repository
Click the **Fork** button at the top right of this page to create your own copy of this repository.

### Step 2: Add your Encrypted Secrets
In your forked repo, go to **Settings** → **Secrets and variables** → **Actions** → click **New repository secret**:

| Secret Name | Required | Description |
|---|---|---|
| `NAUKRI_EMAIL` | **Yes** | Your Naukri login email or mobile number |
| `NAUKRI_PASSWORD` | **Yes** | Your Naukri password |
| `EMAIL_APP_PASSWORD` | **Recommended for Cloud** | 16-character App Password to auto-read Naukri OTP (see below) |
| `RESUME_BASE64` | *Optional* | Your resume file encoded in Base64 (see below) |
| `HEADLINE_VARIANT_A`| *Optional* | Custom headline variant 1 |
| `HEADLINE_VARIANT_B`| *Optional* | Custom headline variant 2 |

> **Why `EMAIL_APP_PASSWORD` is needed for Cloud Automation:**  
> When logging in from Microsoft Azure's cloud datacenter (US), Naukri triggers an **email OTP security challenge**. The script automatically connects to your inbox via IMAP, retrieves the 6-digit OTP code in real time, and logs in!  
> **How to create a free Google App Password (takes 30 seconds):**  
> 1. Go to your [Google Account App Passwords](https://myaccount.google.com/apppasswords) (Make sure 2-Step Verification is turned on).  
> 2. App name: enter `Naukri Bot` → click **Create**.  
> 3. Copy the 16-character code (e.g. `abcd efgh ijkl mnop`) and save it as the **`EMAIL_APP_PASSWORD`** secret.

> **How to get your resume in Base64 (Optional):**  
> Run this one-line command in PowerShell to copy your resume directly to your clipboard:  
> ```powershell
> [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\resume.pdf")) | Set-Clipboard
> ```  
> Then paste it into the `RESUME_BASE64` secret. (If omitted, the script updates your headline only).

### Step 3: Enable Workflows
1. Click the **Actions** tab in your repository.
2. Click the green button: **"I understand my workflows, go ahead and enable them"**.

### Step 4: Run Anytime from your Phone (Optional)
* The cloud runner automatically executes every day at **09:00 AM IST (03:30 UTC)**.
* To run it manually from your phone:
  1. Open the free **GitHub app** on your phone (iOS / Android).
  2. Go to your forked repo → **Actions** → **Refresh Naukri Profile** → tap **Run workflow**.

---

## 💻 Local Windows Setup (Runs on your PC)

If you prefer to run it locally on your Windows machine:

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Credentials (`.env`)
```powershell
copy .env.example .env
```
Fill in your credentials in `.env`:
```env
NAUKRI_EMAIL=your_email@example.com
NAUKRI_PASSWORD=your_password_here
RESUME_PATH="C:/Users/your_username/Documents/resume.pdf"
```

### 3. Test the Script
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
# Schedule at 09:00 AM daily
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

## Features

* **Dual-action Refresh:**
  * **Resume Upload:** Automatically re-uploads your resume file to refresh the *"Resume uploaded"* timestamp.
  * **Headline Toggle:** Updates your Resume Headline (alternating between two custom variants or toggling a trailing dot) to refresh the *"Profile last updated"* timestamp.
* **Production Reliability:**
  * **Automated Retries:** 3 automatic retry attempts with exponential backoff on transient network or CDN timeouts.
  * **Anti-Bot & Stealth:** Uses invisible headed Chrome rendering and stealth techniques to prevent Akamai CDN "Access Denied" blocks.
  * **Catch-up Scheduling:** Configured with Windows Task Scheduler `StartWhenAvailable` — if your PC was asleep or off at 9:00 AM, it runs automatically as soon as you wake it up.
  * **Battery Execution:** Configured to run on laptops even while unplugged.
  * **Windows Desktop Notifications:** Displays a native Windows notification banner upon successful update or failure.
  * **Storage Auto-cleanup:** Automatically purges screenshots and debug DOM snapshots older than 14 days.

---

## Configuration Reference (`.env` or GitHub Secrets)

| Variable | Default | Description |
|---|---|---|
| `NAUKRI_EMAIL` | *(required)* | Your Naukri login email or mobile number |
| `NAUKRI_PASSWORD` | *(required)* | Your Naukri password |
| `RESUME_PATH` | *(empty)* | Local path to your resume file (`.pdf`, `.docx`) |
| `RESUME_BASE64` | *(empty)* | Base64-encoded resume file (ideal for GitHub Actions) |
| `HEADLESS` | `true` | `false` to show the browser window |
| `HEADLINE_VARIANT_A` | *(auto)* | First headline variant (max 250 chars) |
| `HEADLINE_VARIANT_B` | *(auto)* | Second headline variant (max 250 chars) |
| `ACTION_DELAY_MS` | `1500` | Delay between browser actions in milliseconds |
| `MAX_RETRIES` | `3` | Maximum retry attempts per execution |
| `RETRY_DELAY_SEC` | `15` | Seconds to wait between retries (doubles each attempt) |
| `RANDOM_JITTER_MAX_SEC`| `0` | Anti-bot random wait window before starting |
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
* **Screenshots & Snapshots:** On any warning or error, a `.png` screenshot and a `.html` DOM snapshot are saved to `screenshots/` for easy diagnosis.
* **CI Summary:** When running via GitHub Actions, a clean Markdown summary table is automatically attached to the job run.
* **State:** `.state.json` tracks which headline variant was applied last and the timestamp of the last successful run.
