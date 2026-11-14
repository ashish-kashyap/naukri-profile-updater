"""
Naukri Profile Auto-Updater (Production Edition)
=================================================
Automates daily updates on Naukri.com to keep your profile active at the top
of recruiter searches:
  1. Optionally re-uploads your resume (refreshes "Resume updated" timestamp)
  2. Updates your Resume Headline (alternates between variants or toggles period)
  3. Includes retries, Windows desktop notifications, old artifact cleanup,
     and optional startup jitter for scheduled runs.

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
PROFILE_DIR = BASE_DIR / ".browser-profile"

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
        self.interactive_verification_required = False
        self.stop_retries = False
        self.resume_completed = False
        self.headline_completed = False

    def execute_single_attempt(self, attempt_num: int = 1) -> bool:
        """Executes one end-to-end update attempt."""
        if not EMAIL or not PASSWORD:
            self.stop_retries = True
            log.error(
                "Credentials not found! Set NAUKRI_EMAIL and NAUKRI_PASSWORD in .env"
            )
            return False

        if not self.skip_resume and self.resume_path:
            rf = Path(self.resume_path)
            if not rf.exists():
                self.stop_retries = True
                log.error(f"Configured resume file does not exist: {self.resume_path}")
                return False
            if rf.suffix.lower() not in {".pdf", ".doc", ".docx", ".rtf"}:
                self.stop_retries = True
                log.error("Resume must be a PDF, DOC, DOCX, or RTF file.")
                return False
            if rf.stat().st_size > 2 * 1024 * 1024:
                self.stop_retries = True
                log.error("Resume exceeds Naukri's 2 MB upload limit.")
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
            browser_args = ["--window-size=1366,768"]

            # Use the selected browser mode and keep the session profile isolated
            # from the user's everyday Chrome profile.
            if not self.headless:
                browser_args.append("--start-maximized")

            launch_kwargs = {
                "headless": self.headless,
                "slow_mo": 50 if not self.headless else 0,
                "args": browser_args,
                "viewport": {"width": 1366, "height": 768},
                "locale": "en-IN",
                "timezone_id": "Asia/Kolkata",
            }
            if channel:
                launch_kwargs["channel"] = channel

            PROFILE_DIR.mkdir(exist_ok=True)
            context = pw.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_kwargs)
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

                # Step 1: Upload resume (if configured and not skipped)
                if not self.skip_resume and self.resume_path and not self.resume_completed:
                    self.resume_completed = self._upload_resume(page)
                if not self.skip_headline and not self.headline_completed:
                    # Re-navigate after a resume upload; the page can display a
                    # completion overlay or replace parts of the profile DOM.
                    if self.resume_completed:
                        page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=35_000)
                        page.wait_for_timeout(ACTION_DELAY_MS)
                    self.headline_completed = self._update_headline(page)

                requested = []
                if not self.skip_resume and self.resume_path:
                    requested.append(self.resume_completed)
                if not self.skip_headline:
                    requested.append(self.headline_completed)

                overall_success = bool(requested) and all(requested)

                if overall_success and not self.dry_run:
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
                context.close()

    # ------------------------------------------------------------------
    # Login Flow
    # ------------------------------------------------------------------

    def _login(self, page) -> bool:
        # Reuse the authenticated session retained by the persistent profile.
        try:
            page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=35_000)
            page.wait_for_timeout(ACTION_DELAY_MS)
            if ("/mnjuser/" in page.url.lower()
                    and page.locator("text=Resume headline").count() > 0):
                log.info("Existing Naukri session is active; login skipped.")
                return True
        except PlaywrightTimeoutError:
            log.warning("Could not reuse the saved session; attempting a fresh login.")

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
            self.stop_retries = True
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

        log.info("Submitting login form...")

        # Intercept the login API response so we know the server's answer
        login_response_status = []
        def _capture_response(resp):
            if "naukri.com/central-login-services/" in resp.url.lower():
                login_response_status.append(resp.status)
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
            for status in login_response_status:
                log.info(f"Naukri login API response: HTTP {status}")
        else:
            # Endpoint names can change; do not submit a second time just
            # because this listener did not observe the expected API route.
            log.warning("No matching Naukri login API response captured; waiting for the page result.")

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
                body_text = page.locator("body").inner_text(timeout=1500)
                if re.search(r"captcha|select all images|which shape does this image|verify that you are human|security check", body_text, re.I):
                    self.interactive_verification_required = True
                    self._save_diagnostics(page, "verification_required")
                    if self.headless:
                        log.error("Naukri requires interactive verification. Rerun with --visible and complete the challenge manually.")
                        return False

                    log.warning("Naukri requires interactive verification. Complete it in the visible browser; waiting up to 3 minutes.")
                    challenge_deadline = time.time() + 180
                    login_resubmitted_after_challenge = False
                    while time.time() < challenge_deadline:
                        if "/mnjuser/" in page.url.lower() or (
                            "naukri.com" in page.url.lower() and "nlogin" not in page.url.lower()
                        ):
                            log.info("Login succeeded after manual verification.")
                            return True

                        try:
                            challenge_text = page.locator("body").inner_text(timeout=1500)
                        except Exception:
                            page.wait_for_timeout(1000)
                            continue

                        if (not login_resubmitted_after_challenge and
                                not re.search(r"captcha|select all images|which shape does this image|verify that you are human|security check", challenge_text, re.I)):
                            # Some challenge flows return to the login form after
                            # verification and require one final explicit submit.
                            try:
                                submit = page.locator(
                                    "button[type='submit'], .loginButton, button:has-text('Login'), button:has-text('Sign in')"
                                ).first
                                if submit.is_visible():
                                    submit.click(timeout=5000)
                                    page.wait_for_timeout(1500)
                                    login_resubmitted_after_challenge = True
                            except Exception:
                                pass
                        page.wait_for_timeout(1000)

                    log.error("Manual verification was not completed successfully within 3 minutes.")
                    return False
                err_locs = page.locator(".server-err, .err, [class*='error-msg'], .login-error, .cntErr").all_inner_texts()
                clean_errs = [e.strip() for e in err_locs if e.strip()]
                if clean_errs:
                    self.stop_retries = True
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
            # Naukri also has a profile-photo file input earlier in the DOM.
            # Target the resume control explicitly to avoid selecting the photo input.
            file_input = page.locator(
                "#attachCV, input[type='file'][aria-label='Upload resume']"
            ).first
            if file_input.count() == 0:
                update_control = page.locator(
                    "input.dummyUpload[value='Update resume'], button:has-text('Update resume'), "
                    "a:has-text('Update resume'), label:has-text('Update resume')"
                ).first
                update_control.wait_for(state="visible", timeout=5000)
                update_control.click()
                page.wait_for_timeout(1000)
                file_input = page.locator(
                    "#attachCV, input[type='file'][aria-label='Upload resume']"
                ).first

            if file_input.count() == 0:
                raise RuntimeError("Naukri did not expose a resume file input after opening Update resume.")

            # Follow Naukri's visible control first: it opens the resume file
            # chooser and may perform setup that direct input assignment skips.
            upload_button = page.locator("input.dummyUpload[value='Update resume']").first
            selected = False
            if upload_button.count() and upload_button.is_visible():
                try:
                    with page.expect_file_chooser(timeout=4000) as chooser_info:
                        upload_button.click(timeout=5000)
                    chooser_info.value.set_files(str(resume_file))
                    selected = True
                except PlaywrightTimeoutError:
                    log.debug("Update resume control did not open a chooser; assigning the resume input directly.")

            if not selected:
                file_input.set_input_files(str(resume_file))
            log.info("Resume file selected, waiting for upload to complete...")
            # Naukri reports the result in #attachCVMsgBox, not the older
            # .resumeAttachSuccessToast class. Require its success message and
            # verify the filename displayed in the resume card.
            upload_message = page.locator("#attachCVMsgBox .msgBox.success .msg").first
            upload_message.wait_for(state="visible", timeout=25_000)
            message_text = upload_message.inner_text().strip()
            if not re.search(r"resume.*successfully uploaded", message_text, re.I):
                log.error(f"Unexpected Naukri resume upload status: {message_text[:160]}")
                self._save_diagnostics(page, "resume_upload_unverified")
                return False

            displayed_filename = page.locator("#lazyAttachCV .resume-name-inline").inner_text().strip()
            if displayed_filename != resume_file.name:
                log.error("Naukri confirmed an upload, but the profile filename does not match the selected resume.")
                self._save_diagnostics(page, "resume_upload_filename_mismatch")
                return False
            log.info("Resume upload verified from Naukri's success message and profile filename.")
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
            save_buttons = page.get_by_role("button", name=re.compile(r"^\s*save\s*$", re.I))
            for index in range(save_buttons.count() - 1, -1, -1):
                save_btn = save_buttons.nth(index)
                if save_btn.is_visible():
                    save_btn.click(timeout=5000)
                    saved_with_button = True
                    break
        except Exception:
            pass

        if not saved_with_button:
            log.error("Could not find a visible Save button for the headline editor.")
            self._save_diagnostics(page, "headline_save_button_not_found")
            return False

        try:
            textarea.wait_for(state="hidden", timeout=10_000)
            # Naukri's success/upsell dialog is an explicit server confirmation.
            # Treat it as authoritative instead of trying to click the editor
            # underneath the blocking dialog and repeating an already-saved update.
            success_text = page.locator(".profile-updated-container .success-text")
            if success_text.is_visible(timeout=1500) and re.search(
                r"profile updated successfully", success_text.inner_text(), re.I
            ):
                state = load_state()
                state["last_variant"] = "A" if state.get("last_variant", "B") == "B" else "B"
                save_state(state)
                log.info("Headline update confirmed by Naukri's 'Profile updated successfully' message.")
                return True

            # Naukri displays a success/pro upsell modal after saving. Close it
            # before attempting the read-back edit click, which it otherwise blocks.
            success_modal = page.locator(".profileEditDrawer.profileUpdatedProLayer")
            modal_close = success_modal.locator("[aria-label='Close']").first
            if modal_close.is_visible(timeout=1500):
                modal_close.click(timeout=3000, force=True)
            success_modal.wait_for(state="hidden", timeout=5000)
            page.wait_for_timeout(300)
            page.locator("xpath=//span[text()='Resume headline']/following-sibling::span[contains(@class,'edit')]").click(timeout=5000)
            textarea.wait_for(state="visible", timeout=8000)
            if textarea.input_value().strip() != new_headline.strip():
                log.error("Naukri did not persist the requested headline; read-back differs.")
                self._save_diagnostics(page, "headline_readback_mismatch")
                return False
        except Exception as exc:
            log.error(f"Could not verify the saved headline from Naukri: {exc}")
            self._save_diagnostics(page, "headline_readback_failed")
            return False

        state = load_state()
        state["last_variant"] = "A" if state.get("last_variant", "B") == "B" else "B"
        save_state(state)
        log.info("Headline save verified by reading it back from Naukri.")

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
        jitter = random.randint(0, RANDOM_JITTER_MAX_SEC)
        log.info(f"Applying configured startup jitter: waiting {jitter}s before execution...")
        time.sleep(jitter)

    headless = HEADLESS_ENV and not args.visible
    max_retries = args.retries if args.retries is not None else MAX_RETRIES_ENV
    if max_retries < 1:
        log.error("Retry count must be at least 1.")
        return False
    retry_delay = RETRY_DELAY_SEC_ENV

    updater = NaukriUpdater(
        headless=headless,
        dry_run=args.dry_run,
        skip_resume=args.skip_resume,
        skip_headline=args.skip_headline,
        resume_path=args.resume_path,
    )

    if (args.skip_resume and args.skip_headline) or (args.skip_headline and not updater.resume_path):
        log.error("No update action selected. Configure a resume path or enable the headline update.")
        return False

    success = False
    attempts_used = 0
    for attempt in range(1, max_retries + 1):
        attempts_used = attempt
        try:
            success = updater.execute_single_attempt(attempt_num=attempt)
        except Exception as exc:
            log.error(f"Attempt {attempt} could not start or complete: {exc}", exc_info=True)
            success = False
        if success:
            log.info(f"Update succeeded on attempt {attempt}.")
            break
        elif updater.interactive_verification_required or updater.stop_retries:
            if updater.interactive_verification_required:
                log.error("Stopping retries because an interactive verification challenge needs your attention.")
            else:
                log.error("Stopping retries because this failure cannot be fixed by retrying.")
            break
        elif attempt < max_retries:
            log.warning(f"Attempt {attempt} failed. Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

    if success:
        if args.dry_run:
            log.info("Dry run completed. No profile changes were saved.")
            return True
        log.info("Naukri Profile Auto-Updater completed successfully.")
        send_windows_notification(
            title="Naukri Profile Updater",
            message="All requested Naukri profile updates were verified successfully.",
        )
        return True
    else:
        if not args.dry_run:
            state = load_state()
            state["last_attempt"] = datetime.now().isoformat()
            state["last_status"] = "PARTIAL" if updater.resume_completed or updater.headline_completed else "FAILED"
            save_state(state)
        log.error(f"Update did not complete after {attempts_used} attempt(s). Review the logs and screenshots above.")
        send_windows_notification(
            title="Naukri Updater Alert",
            message=f"Profile update did not complete after {attempts_used} attempt(s). Check logs/ for details.",
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
