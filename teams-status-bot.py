#!/usr/bin/python3

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime
import os
import signal
import subprocess
import time


def _pgrep(pattern):
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
        return [int(p) for p in out.split()]
    except subprocess.CalledProcessError:
        return []


# ======================================================================================== #
# ====================================== SETTINGS ======================================== #
# ======================================================================================== #


# Credentials are read from a local .env file (see .env.example). Never commit .env.
def loadEnv(path=".env"):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


loadEnv()
email = os.environ.get("TEAMS_EMAIL")
password = os.environ.get("TEAMS_PASSWORD")
if not email or not password:
    raise SystemExit("Missing TEAMS_EMAIL / TEAMS_PASSWORD. Copy .env.example to .env and fill them in.")

# Status to set: /busy | /available | /away | /berightback | /donotdisturb
status = os.environ.get("TEAMS_STATUS", "/busy")

# The frequency that you want to update your status in minutes
updateEvery = int(os.environ.get("UPDATE_EVERY_MIN", "5"))

# For how long you want to keep this running
forHours = float(os.environ.get("FOR_HOURS", "8"))

# Run Chrome in the background (no visible window)
# headless = False
headless = True

# File where run logs are saved (so you can review previous runs)
LOG_FILE = "bot.log"

# Chrome profile that keeps the Teams login session between runs
PROFILE_DIR = os.path.abspath("chrome-profile")


# ======================================================================================== #
# ======================================== LOGIC ========================================= #
# ======================================================================================== #

driver = None


def log(message):
    # Print to the terminal and append to LOG_FILE so previous runs can be reviewed.
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def ensureSingleInstance():
    # Only one Chrome session can use the profile at a time.
    my_pid = os.getpid()
    bot_pids = [p for p in _pgrep("teams-status-bot.py") if p != my_pid]
    if bot_pids:
        raise SystemExit(
            f"Another bot instance is already running (PID {bot_pids[0]}). "
            "Stop it first, then run again."
        )


def killOrphanChrome():
    # If a previous run was killed without driver.quit(), its Chrome keeps
    # running and holds the profile's SingletonLock, which makes every new
    # launch die with SessionNotCreatedException ("Chrome instance exited").
    # The lock is a symlink to "<hostname>-<pid>" of the Chrome that owns it.
    lock = os.path.join(PROFILE_DIR, "SingletonLock")
    if not os.path.lexists(lock):
        return
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return  # stale lock, owner already gone
    log(f"[setup] Closing orphaned Chrome (PID {pid}) left by a previous run...")
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    os.kill(pid, signal.SIGKILL)


def setupDriver():
    global driver
    killOrphanChrome()
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    driver = webdriver.Chrome(options=options)
    driver.get("https://teams.microsoft.com/")
    driver.implicitly_wait(45)
    driver.set_page_load_timeout(30)
    log("[setup] Chrome launched" + (" (headless)" if headless else "") + ", loading Teams...")


# Maps the status you set at the top to the menu item in Teams.
STATUS_TIDS = {
    "/available": "me_control_presence_availability_available",
    "/busy": "me_control_presence_availability_busy",
    "/donotdisturb": "me_control_presence_availability_do_not_disturb",
    "/berightback": "me_control_presence_availability_be_right_back",
    "/away": "me_control_presence_availability_appear_away",
    "/offline": "me_control_presence_availability_appear_offline",
}


def click(css):
    driver.find_element(By.CSS_SELECTOR, css).click()


def waitAndClick(css, timeout=15):
    # Implicit wait + WebDriverWait stacks delays (each poll can wait 45s).
    driver.implicitly_wait(0)
    try:
        WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, css))
        ).click()
    finally:
        driver.implicitly_wait(45)


def isLoggedIn():
    # When signed in, we land on the Teams app. When not, we get redirected
    # to login.microsoftonline.com or the company's own login server.
    url = driver.current_url
    return "teams.microsoft.com" in url and "login" not in url


def waitForAppShell(timeout=180):
    # isLoggedIn() only checks the URL, which flips to teams.microsoft.com
    # while the app is still on its splash screen and the SPA is booting.
    # If we start clicking the avatar then, the renderer is still pegged
    # running bootstrap JS and can't answer the find_element, so chromedriver
    # raises "timeout: Timed out receiving message from renderer". Wait until
    # the avatar trigger actually exists before doing anything with it.
    log("[setup] Waiting for the Teams app shell to finish loading...")
    driver.implicitly_wait(0)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                if driver.find_elements(
                    By.CSS_SELECTOR, "[data-tid='me-control-avatar-trigger']"
                ):
                    log("[setup] App shell ready.")
                    return True
            except Exception:
                pass  # renderer still busy booting — keep polling
            time.sleep(2)
    finally:
        driver.implicitly_wait(45)
    log(f"[setup] App shell did not appear within {timeout}s — proceeding anyway.")
    return False


def tryAutoLogin(email, password):
    # Best-effort: fill email + password on Microsoft's standard login page.
    # If the org redirects to its own login server or asks for an OTP, this
    # stops early and you finish signing in by hand in the window.
    try:
        log("[login] Filling email...")
        box = driver.find_element(By.NAME, "loginfmt")
        box.send_keys(email)
        box.send_keys(Keys.RETURN)
        time.sleep(3)

        log("[login] Filling password...")
        box = driver.find_element(By.NAME, "passwd")
        box.send_keys(password)
        box.send_keys(Keys.RETURN)
    except Exception as e:
        log(f"[login] Auto-fill stopped ({type(e).__name__}). Finish signing in manually in the browser window.")


def waitUntilLoggedIn(timeout_seconds=300):
    log("[login] Waiting for you to finish signing in (OTP/MFA)... up to "
        f"{timeout_seconds // 60} min.")
    driver.implicitly_wait(0)  # don't let find/url checks block; poll instead
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if isLoggedIn():
            driver.implicitly_wait(45)
            log("[login] Signed in. Session saved to ./chrome-profile.")
            return True
        time.sleep(3)
    driver.implicitly_wait(45)
    return False


def updateStatus(status: str = "/busy"):
    tid = STATUS_TIDS.get(status)
    if tid is None:
        log(f"[status] Unknown status '{status}'. Valid: {', '.join(STATUS_TIDS)}")
        return

    # Dismiss any open menus so the avatar trigger always opens (not closes) the flyout.
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        time.sleep(0.5)
    except Exception:
        pass

    # Open the profile menu, open the status submenu, then pick the status.
    log("[status] Opening profile menu...")
    waitAndClick("[data-tid='me-control-avatar-trigger']")
    log("[status] Opening status submenu...")
    waitAndClick("[data-tid='set-presence-status-menu-item']")
    log(f"[status] Selecting '{status}'...")
    waitAndClick(f"[data-tid='{tid}']")
    log(f"[status] Set to '{status}'")


def keepUpdating(status: str = "/busy", every: int = 5, hours: int = 1):
    total_updates = int((hours * 60) / every)
    log(f"[loop] Will update status '{status}' every {every} min for {hours} h ({total_updates} times)")
    for i in range(total_updates):
        try:
            updateStatus(status)
        except Exception as e:
            # A single transient failure (e.g. a slow renderer) shouldn't tear
            # down the whole multi-hour run — log it and try again next cycle.
            log(f"[loop] Update {i+1} failed ({type(e).__name__}: {e}); "
                "will retry next cycle.")
        if i < total_updates - 1:
            log(f"[loop] {i+1}/{total_updates} done — next update in {every} min")
            time.sleep(every * 60)
    log("[loop] Done.")


def runAutomation(email, password, status, every, hours):
    log("=" * 50)
    log("[run] Starting new run")
    ensureSingleInstance()
    try:
        setupDriver()
        time.sleep(5)  # let any login redirect settle

        if not isLoggedIn():
            if headless:
                driver.save_screenshot("login_state.png")
                log("[login] Not signed in and headless=True — can't do OTP in the "
                    "background. Set headless=False at the top, run once to "
                    "sign in by hand, then switch back to headless=True.")
                return
            tryAutoLogin(email, password)
            if not waitUntilLoggedIn(300):
                log("[login] Timed out waiting for sign-in. Exiting.")
                return

        # URL says we're "logged in" the moment Teams starts loading, but the
        # app shell (and the avatar we click) isn't rendered yet. Wait for it.
        waitForAppShell()
        keepUpdating(status=status, every=every, hours=hours)
    except KeyboardInterrupt:
        log("[stopped] Interrupted by user (Ctrl+C).")
    except Exception as e:
        log(f"[error] {type(e).__name__}: {e}")
        try:
            driver.save_screenshot("error_state.png")
            log("[error] Screenshot saved to error_state.png")
        except Exception:
            pass
    finally:
        if driver:
            driver.quit()
            log("[done] Browser closed.")


# MAIN RUNNING POINT OF THIS APP
runAutomation(
    email=email,
    password=password,
    status=status,
    every=updateEvery,
    hours=forHours
)
