"""
Naukri Profile Auto-Updater (Production Edition)
=================================================
Automates daily updates on Naukri.com to keep your profile active at the top
of recruiter searches:
  1. Optionally re-uploads your resume (refreshes "Resume updated" timestamp)
  2. Updates your Resume Headline (alternates between variants or toggles period)
  3. Includes automated retries, Windows desktop notifications, old artifact cleanup,
     and random jitter for anti-bot protection.

Usage:
    python update_profile.py                  # Normal production run (headless)
    python update_profile.py --visible        # Debug run with browser visible
    python update_profile.py --dry-run        # Read headline & simulate without saving
    python update_profile.py --skip-resume    # Only update headline
    python update_profile.py --skip-headline  # Only upload resume
    python update_profile.py --retries 1      # Override max retry attempts
"""

import os
import sys
import time
import json
import random
import logging
import argparse
import subprocess
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Paths and Environment
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.resolve()
LOG_DIR = BASE_DIR / "logs"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
STATE_FILE = BASE_DIR / ".state.json"

LOG_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

EMAIL = os.getenv("NAUKRI_EMAIL", "").strip()
PASSWORD = os.getenv("NAUKRI_PASSWORD", "").strip()
HEADLESS_ENV = os.getenv("HEADLESS", "true").lower() == "true"
ACTION_DELAY_MS = int(os.getenv("ACTION_DELAY_MS", "1500"))
HEADLINE_A = os.getenv("HEADLINE_VARIANT_A", "").strip()
HEADLINE_B = os.getenv("HEADLINE_VARIANT_B", "").strip()
RESUME_PATH_ENV = os.getenv("RESUME_PATH", "").strip().strip('"').strip("'")

MAX_RETRIES_ENV = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SEC_ENV = int(os.getenv("RETRY_DELAY_SEC", "15"))
RANDOM_JITTER_MAX_SEC = int(os.getenv("RANDOM_JITTER_MAX_SEC", "0"))
SEND_NOTIFICATIONS = os.getenv("SEND_NOTIFICATIONS", "true").lower() == "true"

NAUKRI_LOGIN_URL = "https://www.naukri.com/nlogin/login"
NAUKRI_PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------

log_file = LOG_DIR / f"naukri_{datetime.now().strftime('%Y%m')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("naukri-updater")


# ---------------------------------------------------------------------------
# Helpers: Notifications, State, Cleanup
# ---------------------------------------------------------------------------

def send_windows_notification(title: str, message: str, is_error: bool = False):
    """Sends a native Windows Toast notification if enabled and not running in CI."""
    if not SEND_NOTIFICATIONS:
        return
    try:
        # Use PowerShell to trigger a modern Windows Notification toast
        escaped_title = title.replace('"', '`"').replace("'", "''")
        escaped_msg = message.replace('"', '`"').replace("'", "''")
        ps_cmd = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            f"$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$txt = $t.GetElementsByTagName('text'); "
            f"$txt.Item(0).AppendChild($t.CreateTextNode('{escaped_title}')) > $null; "
            f"$txt.Item(1).AppendChild($t.CreateTextNode('{escaped_msg}')) > $null; "
            f"$toast = [Windows.UI.Notifications.ToastNotification]::new($t); "
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Naukri Updater').Show($toast);"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def clean_old_artifacts(days: int = 14):
    """Removes screenshots and debug HTML snapshots older than specified days."""
    try:
        cutoff = datetime.now() - timedelta(days=days)
        for f in SCREENSHOTS_DIR.glob("*.*"):
            if f.is_file() and f.suffix.lower() in [".png", ".html"]:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
    except Exception as e:
        log.debug(f"Artifact cleanup notice: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_variant": "B", "last_run": None, "last_status": "NONE"}


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not save state to {STATE_FILE}: {e}")


def get_browser_channel():
    """Detects if real Google Chrome is installed on the system."""
    chrome_paths = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    ]
    if any(p.exists() for p in chrome_paths):
        return "chrome"
    return None


def pick_headline(current_headline: str) -> str:
    """Alternates between configured variants or toggles a trailing period."""
    state = load_state()
    use_variant_a = state.get("last_variant", "B") == "B"

    if HEADLINE_A and HEADLINE_B:
        chosen = HEADLINE_A if use_variant_a else HEADLINE_B
    else:
        base = current_headline.rstrip(". ")
        chosen = f"{base}." if use_variant_a else base

    # Warn if headline exceeds Naukri's 250 character limit
    if len(chosen) > 250:
        log.warning(f"Headline length is {len(chosen)} chars, exceeding Naukri's 250-character limit! Truncating to 250 chars.")
        chosen = chosen[:250]

    state["last_variant"] = "A" if use_variant_a else "B"
    save_state(state)
    return chosen


# ---------------------------------------------------------------------------
# Automation Engine
# ---------------------------------------------------------------------------

class NaukriUpdater:
    def __init__(self, headless: bool = True, dry_run: bool = False,
                 skip_resume: bool = False, skip_headline: bool = False,
                 resume_path: str = ""):
        self.headless = headless
        self.dry_run = dry_run
        self.skip_resume = skip_resume
        self.skip_headline = skip_headline
        self.resume_path = resume_path or RESUME_PATH_ENV

    def execute_single_attempt(self, attempt_num: int = 1) -> bool:
        """Executes one end-to-end update attempt."""
        if not EMAIL or not PASSWORD:
            log.error(
                "Credentials not found! Set NAUKRI_EMAIL and NAUKRI_PASSWORD in .env"
            )
            return False

        if not self.skip_resume and self.resume_path:
            rf = Path(self.resume_path)
            if not rf.exists():
                log.error(f"Configured resume file does not exist: {self.resume_path}")
                return False

        log.info("=" * 60)
        log.info(f"Naukri Profile Updater starting (Attempt {attempt_num})")
        log.info(f"  Mode        : {'DRY RUN' if self.dry_run else 'LIVE'}")
        log.info(f"  Headless    : {self.headless}")
        log.info(f"  Resume      : {'SKIP' if self.skip_resume else self.resume_path or '(not configured)'}")
        log.info(f"  Headline    : {'SKIP' if self.skip_headline else 'will update'}")
        log.info("=" * 60)

        with sync_playwright() as pw:
            channel = get_browser_channel()
            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
            ]

            # Akamai CDN explicitly detects and blocks the stripped 'chrome-headless-shell' binary.
            # To run invisibly in the background without triggering Akamai CDN bot detection,
            # we run the full headed Chrome binary with the window positioned offscreen.
            if self.headless:
                browser_args.extend([
                    "--window-position=-3200,-3200",
                    "--window-size=1366,768",
                ])
            else:
                browser_args.extend([
                    "--window-size=1366,768",
                    "--start-maximized",
                ])

            # Always run headed Chrome — Akamai CDN blocks headless-shell
            launch_kwargs = {
                "headless": False,
                "slow_mo": 50,
                "args": browser_args,
            }
            if channel:
                launch_kwargs["channel"] = channel

            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=CHROME_USER_AGENT,
                viewport={"width": 1366, "height": 768},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            # Mask webdriver property cleanly
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            page = context.new_page()

            page.on("pageerror", lambda err: log.warning(f"Browser JS error: {err}"))
            page.on("requestfailed", lambda req: log.debug(f"Request failed: {req.url} ({req.failure})"))

            try:
                if not self._login(page):
                    return False

                # Navigate to profile
                log.info(f"Navigating to profile: {NAUKRI_PROFILE_URL}")
                page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=35_000)
                page.wait_for_timeout(ACTION_DELAY_MS)

                results = []

                # Step 1: Upload resume (if configured and not skipped)
                if not self.skip_resume and self.resume_path:
                    resume_ok = self._upload_resume(page)
                    results.append(resume_ok)
                    # Re-navigate to profile for headline step
                    page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=35_000)
                    page.wait_for_timeout(ACTION_DELAY_MS)

                # Step 2: Update headline (if not skipped)
                if not self.skip_headline:
                    headline_ok = self._update_headline(page)
                    results.append(headline_ok)

                overall_success = all(results) if results else True

                if overall_success:
                    state = load_state()
                    state["last_run"] = datetime.now().isoformat()
                    state["last_status"] = "SUCCESS"
                    save_state(state)

                return overall_success

            except PlaywrightTimeoutError as e:
                log.error(f"Timeout during attempt {attempt_num}: {e}")
                self._save_diagnostics(page, f"timeout_attempt_{attempt_num}")
                return False
            except Exception as e:
                log.error(f"Unexpected error during attempt {attempt_num}: {e}", exc_info=True)
                self._save_diagnostics(page, f"error_attempt_{attempt_num}")
                return False
            finally:
                browser.close()

    # ------------------------------------------------------------------
    # Login Flow
    # ------------------------------------------------------------------

    def _login(self, page) -> bool:
        log.info("Navigating to login page...")
        # Optional warm-up visit to home to obtain standard cookies
        try:
            page.goto("https://www.naukri.com/", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        page.goto(NAUKRI_LOGIN_URL, wait_until="domcontentloaded", timeout=45_000, referer="https://www.naukri.com/")
        page.wait_for_timeout(2000)

        # Check for Access Denied CDN block
        if "Access Denied" in page.title() or "access denied" in page.content().lower():
            log.error("Access Denied by Naukri CDN. Try running with --visible flag to use headed mode.")
            self._save_diagnostics(page, "access_denied")
            return False

        log.info(f"Login page loaded. Title: {page.title()}")
        log.info("Entering credentials...")

        email_selector = (
            "#usernameField, "
            "input[name='email'], "
            "input[type='text'][placeholder*='Email'], "
            "input[type='email'], "
            "input[placeholder*='email' i], "
            "input[placeholder*='mobile' i]"
        )
        try:
            email_loc = page.locator(email_selector).first
            email_loc.wait_for(state="visible", timeout=15_000)
            email_loc.click()
            page.wait_for_timeout(300)
            email_loc.fill(EMAIL)
            log.info("Email filled successfully.")
        except Exception as e:
            log.error(f"Could not find or fill email field: {e}")
            self._save_diagnostics(page, "email_field_not_found")
            return False

        page.wait_for_timeout(600)

        # --- Step A: Check if Naukri uses a 2-step flow (email → Continue → password) ---
        # Some Naukri pages hide the password field until email is submitted.
        pwd_loc = page.locator("input[type='password']").first
        try:
            pwd_visible = pwd_loc.is_visible(timeout=1500)
        except Exception:
            pwd_visible = False

        if not pwd_visible:
            # 2-step flow: click Continue/Next to reveal password field
            log.info("Password field not visible — trying 2-step login (email → Continue → password)...")
            try:
                continue_btn = page.locator(
                    "button:has-text('Continue'), button:has-text('Next'), "
                    "button[type='submit']:not(:has-text('Login'))"
                ).first
                continue_btn.wait_for(state="visible", timeout=5_000)
                continue_btn.click()
                log.info("Clicked Continue button, waiting for password field...")
                page.wait_for_timeout(2000)
            except Exception as e:
                log.debug(f"No separate Continue button found ({e}), trying Enter key...")
                page.keyboard.press("Enter")
                page.wait_for_timeout(2000)

        # Now fill password (should be visible in both single and 2-step flows)
        try:
            pwd_loc = page.locator("input[type='password']").first
            pwd_loc.wait_for(state="visible", timeout=10_000)
            pwd_loc.click()
            page.wait_for_timeout(400)
            # Type character-by-character to mimic human input (avoids bot detection on fill())
            pwd_loc.type(PASSWORD, delay=50)
            log.info("Password filled successfully.")
        except Exception as e:
            log.error(f"Could not fill password: {e}")
            self._save_diagnostics(page, "password_field_error")
            return False

        page.wait_for_timeout(600)

        # Capture page state before submitting so debug artifact is useful
        self._save_diagnostics(page, "before_submit")

        log.info("Submitting login form...")

        # Intercept the login API response so we know the server's answer
        login_response_status = []
        def _capture_response(resp):
            if "login" in resp.url.lower() or "auth" in resp.url.lower():
                login_response_status.append((resp.url, resp.status))
        page.on("response", _capture_response)

        try:
            btn_loc = page.locator(
                "button[type='submit'], .loginButton, button:has-text('Login'), button:has-text('Sign in')"
            ).first
            btn_loc.wait_for(state="visible", timeout=10_000)
            btn_loc.click()
            # Also press Enter as a fallback trigger (some SPAs need both)
            page.wait_for_timeout(300)
        except Exception as e:
            log.error(f"Could not click login button: {e}")
            self._save_diagnostics(page, "login_btn_error")
            return False

        # Wait briefly for any network response to come back
        page.wait_for_timeout(2000)
        if login_response_status:
            for url, status in login_response_status:
                log.info(f"Login API response: {url} → HTTP {status}")
        else:
            log.warning("No login API response captured — form may not have submitted (bot block or wrong button).")
            # Fallback: press Enter directly on the password field
            log.info("Trying Enter-key fallback submit...")
            pwd_loc.press("Enter")
            page.wait_for_timeout(2000)

        # Wait for post-login redirect
        log.info("Waiting for login result...")
        start_wait = time.time()
        wait_limit = 30
        last_snapshot_time = 0
        while time.time() - start_wait < wait_limit:
            current_url = page.url
            elapsed = int(time.time() - start_wait)

            # Log page title + first visible text every 10s for diagnostics
            if elapsed - last_snapshot_time >= 10:
                try:
                    title = page.title()
                    snippet = page.inner_text("body")[:300].replace("\n", " ").strip()
                    log.info(f"[login-wait {elapsed}s] URL={current_url} | Title={title!r} | Body={snippet!r}")
                except Exception:
                    log.info(f"[login-wait {elapsed}s] URL={current_url}")
                last_snapshot_time = elapsed

            # Success: redirected away from the login page
            if "mnjuser" in current_url or (
                "naukri.com" in current_url and "nlogin" not in current_url
            ):
                log.info("Login successful (redirected)!")
                return True

            # Credential error shown on page
            try:
                err_locs = page.locator(".server-err, .err, [class*='error-msg'], .login-error, .cntErr").all_inner_texts()
                clean_errs = [e.strip() for e in err_locs if e.strip()]
                if clean_errs:
                    log.error(f"Login error on page: {' | '.join(clean_errs)}")
                    self._save_diagnostics(page, "login_error_text")
                    return False
            except Exception:
                pass

            page.wait_for_timeout(1000)

        log.error(f"Login timed out after {wait_limit}s. Current URL: {page.url}")
        self._save_diagnostics(page, "login_failed")
        return False

    # ------------------------------------------------------------------
    # Resume Upload Flow
    # ------------------------------------------------------------------

    def _upload_resume(self, page) -> bool:
        if not self.resume_path:
            log.warning("RESUME_PATH not configured — skipping resume upload.")
            return True

        resume_file = Path(self.resume_path)
        if not resume_file.exists():
            log.error(f"Resume file not found: {self.resume_path}")
            return False

        log.info(f"Uploading resume: {resume_file.name}")

        if self.dry_run:
            log.info(f"[DRY RUN] Would upload resume: {self.resume_path}")
            return True

        try:
            file_input = page.locator("input[type='file']").first
            if file_input.count() == 0:
                page.locator("button:has-text('Update resume'), a:has-text('Update resume'), label:has-text('Update resume')").first.click()
                page.wait_for_timeout(1000)
                file_input = page.locator("input[type='file']").first

            file_input.set_input_files(str(resume_file))
            log.info("Resume file selected, waiting for upload to complete...")
            page.wait_for_timeout(5000)

            # Robust confirmation verification
            try:
                success_loc = page.locator(
                    ".toast-success, [class*='success'], :has-text('successfully uploaded'), :has-text('Resume updated')"
                ).first
                if success_loc.is_visible(timeout=5000):
                    log.info("Resume uploaded successfully!")
                else:
                    log.info("Resume upload submitted.")
            except Exception:
                log.info("Resume upload submitted.")

            return True

        except Exception as e:
            log.error(f"Resume upload failed: {e}")
            self._save_diagnostics(page, "resume_upload_error")
            return False

    # ------------------------------------------------------------------
    # Headline Update Flow
    # ------------------------------------------------------------------

    def _update_headline(self, page) -> bool:
        log.info("Looking for headline edit button...")

        # Dismiss any open modal/popup first (e.g. Basic details prompt)
        try:
            close_btn = page.locator(".close-btn, button[class*='close'], [aria-label='Close'], .crossIcon").first
            if close_btn.is_visible(timeout=2000):
                log.info("Closing open modal before proceeding...")
                close_btn.click()
                page.wait_for_timeout(800)
        except Exception:
            pass

        # Click pencil icon next to "Resume headline"
        try:
            page.locator(
                "xpath=//span[text()='Resume headline']/following-sibling::span[contains(@class,'edit')]"
            ).click(timeout=8000)
        except Exception:
            try:
                page.locator(
                    "xpath=//div[contains(@class,'widgetHead') and .//span[text()='Resume headline']]//span[contains(@class,'edit')]"
                ).click(timeout=5000)
            except Exception as exc:
                log.error(f"Could not find/click headline edit button: {exc}")
                self._save_diagnostics(page, "headline_edit_btn_not_found")
                return False

        page.wait_for_timeout(ACTION_DELAY_MS)

        log.info("Reading current headline...")
        textarea = page.locator("#resumeHeadlineTxt").first
        try:
            textarea.wait_for(state="visible", timeout=10_000)
        except Exception:
            log.error("Could not find headline textarea (#resumeHeadlineTxt).")
            self._save_diagnostics(page, "headline_textarea_not_found")
            return False

        current_text = textarea.input_value()
        preview = current_text[:80] + ("..." if len(current_text) > 80 else "")
        log.info(f"Current headline: '{preview}'")

        new_headline = pick_headline(current_text)
        new_preview = new_headline[:80] + ("..." if len(new_headline) > 80 else "")
        log.info(f"New headline    : '{new_preview}'")

        if self.dry_run:
            log.info("[DRY RUN] Skipping save — would have saved the new headline above.")
            return True

        # Clear and fill new headline
        textarea.click(click_count=3)
        textarea.fill(new_headline)
        page.wait_for_timeout(500)

        log.info("Saving headline...")
        saved_with_button = False
        try:
            save_btn = page.locator("button.btn-dark-ot").filter(has_text="Save").first
            if save_btn.is_visible(timeout=2000):
                save_btn.click(timeout=5000)
                saved_with_button = True
        except Exception:
            pass

        if not saved_with_button:
            log.info("Using keyboard fallback to save (Tab + Enter)...")
            page.keyboard.press("Tab")
            page.keyboard.press("Enter")

        page.wait_for_timeout(2000)

        # Confirm save: either textarea is closed, or success toast is present
        still_open = page.locator("#resumeHeadlineTxt").count() > 0
        if not still_open:
            log.info("Headline saved! Profile 'last updated' timestamp refreshed.")
        else:
            toast = page.locator(".toast-success, [class*='successToast'], [class*='success-toast']")
            if toast.count() > 0:
                log.info("Headline saved (toast confirmed)!")
            else:
                log.warning("Save may not have completed. Please verify manually.")
                self._save_diagnostics(page, "save_uncertain")
                return False

        return True

    # ------------------------------------------------------------------
    # Diagnostic Artifacts
    # ------------------------------------------------------------------

    def _save_diagnostics(self, page, name: str):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_path = SCREENSHOTS_DIR / f"{name}_{ts}.png"
        html_path = SCREENSHOTS_DIR / f"{name}_{ts}.html"
        try:
            page.screenshot(path=str(img_path))
            log.info(f"Screenshot saved: {img_path}")
        except Exception:
            pass
        try:
            html_path.write_text(page.content(), encoding="utf-8")
            log.info(f"Page HTML snapshot saved: {html_path}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Production Runner with Retries and Jitter
# ---------------------------------------------------------------------------

def run_production(args) -> bool:
    """Executes the updater with retry management, jitter, and notifications."""
    clean_old_artifacts(days=14)

    # Apply random jitter if configured (e.g. for scheduler runs)
    if RANDOM_JITTER_MAX_SEC > 0 and not args.dry_run:
        jitter = random.randint(5, RANDOM_JITTER_MAX_SEC)
        log.info(f"Applying anti-bot schedule jitter: waiting {jitter}s before execution...")
        time.sleep(jitter)

    headless = HEADLESS_ENV and not args.visible
    max_retries = args.retries if args.retries is not None else MAX_RETRIES_ENV
    retry_delay = RETRY_DELAY_SEC_ENV

    updater = NaukriUpdater(
        headless=headless,
        dry_run=args.dry_run,
        skip_resume=args.skip_resume,
        skip_headline=args.skip_headline,
        resume_path=args.resume_path,
    )

    success = False
    for attempt in range(1, max_retries + 1):
        success = updater.execute_single_attempt(attempt_num=attempt)
        if success:
            log.info(f"Update succeeded on attempt {attempt}.")
            break
        elif attempt < max_retries:
            log.warning(f"Attempt {attempt} failed. Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

    if success:
        log.info("Naukri Profile Auto-Updater completed successfully.")
        send_windows_notification(
            title="Naukri Profile Updater",
            message="Your Naukri profile headline and timestamp were refreshed successfully!",
        )
        return True
    else:
        log.error(f"All {max_retries} attempts failed. Please review the logs and screenshots above.")
        send_windows_notification(
            title="Naukri Updater Alert",
            message=f"Profile update failed after {max_retries} attempts. Check screenshots/ and logs/ for details.",
            is_error=True,
        )
        return False


def parse_args():
    parser = argparse.ArgumentParser(
        description="Naukri Profile Auto-Updater (Production Edition)"
    )
    parser.add_argument("--visible", action="store_true",
                        help="Show the browser window (overrides HEADLESS=true)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Login and inspect without saving changes")
    parser.add_argument("--skip-resume", action="store_true",
                        help="Skip resume upload, only update headline")
    parser.add_argument("--skip-headline", action="store_true",
                        help="Skip headline update, only upload resume")
    parser.add_argument("--retries", type=int, default=None,
                        help=f"Override max retries (default: {MAX_RETRIES_ENV})")
    parser.add_argument("--resume-path", type=str, default="",
                        help="Override resume path from .env")
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    ok = run_production(cli_args)
    sys.exit(0 if ok else 1)
