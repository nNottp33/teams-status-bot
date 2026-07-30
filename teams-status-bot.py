#!/usr/bin/python3

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime
import os
import shutil
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
                    value = value.strip()
                    # Strip matching surrounding quotes like dotenv/compose do,
                    # else they get typed into the password field verbatim.
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    os.environ.setdefault(key.strip(), value)


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

# Run Chrome in the background (no visible window).
# HEADLESS=0 shows the window — needed for the interactive MFA/OTP sign-in
# that refreshes ./chrome-profile. No file edit required for that run.
headless = os.environ.get("HEADLESS", "1") != "0"

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
    # In Docker, Selenium Manager has no linux/arm64 build, so the image sets
    # CHROMEDRIVER_PATH to the system chromedriver. Unset = old behavior.
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    if driver_path:
        from selenium.webdriver.chrome.service import Service
        driver = webdriver.Chrome(service=Service(driver_path), options=options)
    else:
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
    url = driver.current_url or ""  # None while the page is mid-navigation
    return "teams.microsoft.com" in url and "login" not in url


class SessionExpired(Exception):
    pass


def currentPresence():
    # Authoritative readout of what the SERVER thinks: Teams writes it into the
    # avatar trigger's aria-label, e.g. "Your profile, status Available".
    driver.implicitly_wait(0)
    try:
        el = driver.find_elements(
            By.CSS_SELECTOR, "[data-tid='me-control-avatar-trigger']"
        )
        return (el[0].get_attribute("aria-label") or "") if el else ""
    except Exception:
        return ""
    finally:
        driver.implicitly_wait(45)


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


def dumpFailureState(tag):
    # When a status update times out, the chromedriver stack tells us nothing
    # about what the page actually looked like. Capture the URL + a screenshot
    # so the NEXT failure is a decisive breadcrumb (open flyout = slow render;
    # no menu = avatar toggle problem; login page = session/throttle).
    try:
        log(f"[status] Failure state — url={driver.current_url}")
    except Exception:
        pass
    try:
        shot = f"fail_{tag}_{datetime.now():%H%M%S}.png"
        driver.save_screenshot(shot)
        log(f"[status] Saved {shot}")
    except Exception:
        pass


def openStatusMenu(status, tid):
    # Open the profile menu, open the status submenu, then pick the status.
    # The submenu step gets a longer wait: on a headless tab that's been idle
    # for minutes the renderer is throttled and the flyout can paint slowly.
    log("[status] Opening profile menu...")
    waitAndClick("[data-tid='me-control-avatar-trigger']")
    log("[status] Opening status submenu...")
    waitAndClick("[data-tid='set-presence-status-menu-item']", timeout=30)
    log(f"[status] Selecting '{status}'...")
    waitAndClick(f"[data-tid='{tid}']")
    # "the click didn't raise" is NOT success: the presence write is server
    # side and gets dropped silently when the saved session is stale. Teams
    # then boots from cache (shell renders, chats look normal) but reports
    # presence "Unknown" — which is how this bot logged "Set to '/available'"
    # every 5 min for weeks while nobody saw the status change. Only trust the
    # readout, and only treat "Unknown" as fatal: the wording of the other
    # states is not verified, so matching them could fail a working run.
    time.sleep(2)  # let the badge repaint after the write
    presence = currentPresence()
    if "Unknown" in presence:
        raise SessionExpired(
            f"clicked '{status}' but presence still reads {presence!r} — the "
            "saved session in ./chrome-profile is stale, so Teams drops every "
            "presence write. Fix: docker compose down, set headless=False, run "
            "`python teams-status-bot.py` once and sign in by hand (MFA/OTP), "
            "set headless=True, then docker compose up -d --build."
        )
    log(f"[status] Set to '{status}' — avatar now reads {presence!r}")


def updateStatus(status: str = "/busy"):
    tid = STATUS_TIDS.get(status)
    if tid is None:
        log(f"[status] Unknown status '{status}'. Valid: {', '.join(STATUS_TIDS)}")
        return

    # The flyout open/click sequence flakes intermittently (a throttled idle
    # renderer, or a stray menu left open so the avatar click toggles it shut).
    # Retry the whole sequence within the cycle instead of losing 5 minutes.
    attempts = 2
    for attempt in range(1, attempts + 1):
        # Teams' MSAL sometimes redirects the whole tab to login.microsoftonline.com
        # to fetch a token for a sub-app (Viva Engage) with
        # redirect_uri=brk-multihub://, a native-broker scheme plain Chrome can't
        # follow. The flow then never completes and the tab is stranded on the auth
        # page for good — same client-request-id for every later cycle — so clicking
        # the avatar can only ever time out. Navigate back to Teams to recover.
        if not isLoggedIn():
            log("[status] Tab left Teams (auth redirect) — reloading Teams...")
            driver.get("https://teams.microsoft.com/")
            waitForAppShell()
        # Dismiss any open menus so the avatar trigger always opens (not closes)
        # the flyout.
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except Exception:
            pass
        try:
            openStatusMenu(status, tid)
            return
        except SessionExpired:
            raise  # retrying can't refresh a stale session
        except Exception as e:
            log(f"[status] Attempt {attempt}/{attempts} failed "
                f"({type(e).__name__}).")
            dumpFailureState(f"attempt{attempt}")
            if attempt == attempts:
                raise
            time.sleep(2)


def keepUpdating(status: str = "/busy", every: int = 5, hours: int = 1):
    total_updates = int((hours * 60) / every)
    log(f"[loop] Will update status '{status}' every {every} min for {hours} h ({total_updates} times)")
    for i in range(total_updates):
        try:
            updateStatus(status)
        except SessionExpired:
            # Retrying can't help: only an interactive sign-in refreshes the
            # profile. Stop the run so the reason is the last thing in the log.
            raise
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
        try:
            keepUpdating(status=status, every=every, hours=hours)
        except SessionExpired:
            # A stale session still boots Teams from the service-worker cache:
            # the URL stays teams.microsoft.com, so isLoggedIn() passes and the
            # login flow never runs — even after hitting the AAD logout URL
            # (verified: the shell was back in 10s with no login redirect).
            # The only reliable reset is a fresh profile. Non-headless is the
            # interactive repair run: park the old profile, sign in clean.
            if headless:
                raise
            log("[login] Stale session — parking the old profile and signing in fresh...")
            driver.quit()
            stale = PROFILE_DIR + ".stale"
            if os.path.exists(stale):
                shutil.rmtree(stale)
            os.rename(PROFILE_DIR, stale)
            setupDriver()
            time.sleep(5)
            if not isLoggedIn():
                tryAutoLogin(email, password)
                if not waitUntilLoggedIn(300):
                    log("[login] Timed out waiting for sign-in. Exiting.")
                    return
            waitForAppShell()
            keepUpdating(status=status, every=every, hours=hours)
    except KeyboardInterrupt:
        log("[stopped] Interrupted by user (Ctrl+C).")
    except SessionExpired as e:
        log(f"[error] SessionExpired: {e}")
        raise  # __main__ turns this into exit 2 so the scheduler backs off
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
if __name__ == "__main__":
    try:
        runAutomation(
            email=email,
            password=password,
            status=status,
            every=updateEvery,
            hours=forHours
        )
    except SessionExpired:
        raise SystemExit(2)  # entrypoint.sh backs off instead of relaunching
