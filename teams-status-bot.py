#!/usr/bin/python3

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from datetime import datetime
import os
import time


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


def setupDriver():
    global driver
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-data-dir=./chrome-profile")
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


def isLoggedIn():
    # When signed in, we land on the Teams app. When not, we get redirected
    # to login.microsoftonline.com or the company's own login server.
    url = driver.current_url
    return "teams.microsoft.com" in url and "login" not in url


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

    # Open the profile menu, open the status submenu, then pick the status.
    click("[data-tid='me-control-avatar-trigger']")
    time.sleep(1)
    click("[data-tid='set-presence-status-menu-item']")
    time.sleep(1)
    click(f"[data-tid='{tid}']")
    log(f"[status] Set to '{status}'")


def keepUpdating(status: str = "/busy", every: int = 5, hours: int = 1):
    total_updates = int((hours * 60) / every)
    log(f"[loop] Will update status '{status}' every {every} min for {hours} h ({total_updates} times)")
    for i in range(total_updates):
        updateStatus(status)
        if i < total_updates - 1:
            log(f"[loop] {i+1}/{total_updates} done — next update in {every} min")
            time.sleep(every * 60)
    log("[loop] Done.")


def runAutomation(email, password, status, every, hours):
    log("=" * 50)
    log("[run] Starting new run")
    setupDriver()
    time.sleep(5)  # let any login redirect settle

    if not isLoggedIn():
        if headless:
            driver.save_screenshot("login_state.png")
            log("[login] Not signed in and headless=True — can't do OTP in the "
                "background. Set headless=False at the top, run once to "
                "sign in by hand, then switch back to headless=True.")
            driver.quit()
            return
        tryAutoLogin(email, password)
        if not waitUntilLoggedIn(300):
            log("[login] Timed out waiting for sign-in. Exiting.")
            driver.quit()
            return

    keepUpdating(status=status, every=every, hours=hours)
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
