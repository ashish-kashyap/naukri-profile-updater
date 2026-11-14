# Naukri Profile Auto-Updater

## Overview
A Python + Playwright script that logs into Naukri.com daily and updates your resume headline, refreshing the "last updated" timestamp so your profile stays visible to recruiters.

## Project Structure
```
naukri-profile-updater/
├── update_profile.py       # Main automation script
├── setup_scheduler.py      # Windows Task Scheduler setup script
├── requirements.txt        # Python dependencies
├── .env                    # Credentials (gitignored)
├── .env.example            # Template for .env
├── .gitignore              # Ignores .env and logs
└── logs/
    └── naukri_updater.log  # Daily run logs
```

## How It Works
1. Reads credentials from `.env`
2. Launches Playwright browser (headless by default)
3. Logs into Naukri.com
4. Navigates to profile → Edit Resume Headline
5. Alternates between two headline variants (adds/removes a trailing dot) to force a save
6. Logs the result with timestamp
7. Task Scheduler runs it daily at a configurable time

## Verification Plan
- Manual run first to confirm login and edit works
- Check `logs/naukri_updater.log` for success/failure
- Verify Task Scheduler task is created
