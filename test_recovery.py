#!/usr/bin/env python3
"""Checks updateStatus() reloads Teams when the tab got stranded on the auth page.

Run: python test_recovery.py
"""
import importlib.util
import os

os.environ.setdefault("TEAMS_EMAIL", "test@example.com")
os.environ.setdefault("TEAMS_PASSWORD", "test")

spec = importlib.util.spec_from_file_location("bot", "teams-status-bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class FakeDriver:
    """Stands in for the WebDriver: records get(), reports a fixed current_url."""

    def __init__(self, url):
        self.current_url = url
        self.visited = []

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def implicitly_wait(self, _):
        pass

    def find_element(self, *_):
        raise Exception("no element")  # forces the retry path, harmless here

    def find_elements(self, *_):
        return ["avatar"]  # app shell "ready" so waitForAppShell returns fast


def run(url):
    bot.driver = FakeDriver(url)
    bot.log = lambda *_: None
    try:
        bot.updateStatus("/busy")
    except Exception:
        pass  # clicking always fails with the stub; we only assert on navigation
    return bot.driver.visited


stranded = "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?x=1"
assert run(stranded) == ["https://teams.microsoft.com/"], "stranded tab was not reloaded"
assert run("https://teams.microsoft.com/v2/") == [], "healthy tab should not reload"


class BounceDriver(FakeDriver):
    def get(self, url):
        self.visited.append(url)
        self.current_url = stranded  # AAD sends the tab right back to login


bot.driver = BounceDriver(stranded)
bot.log = lambda *_: None
bot.waitForAppShell = lambda *a, **k: False  # skip the real 180s poll
try:
    bot.updateStatus("/busy")
except bot.SessionExpired:
    pass
else:
    raise AssertionError("dead session was not detected as SessionExpired")


# get() itself can raise ("Timed out receiving message from renderer"); that
# must not skip the dead-session check.
class RenderTimeoutDriver(FakeDriver):
    def get(self, url):
        raise Exception("timeout: Timed out receiving message from renderer")


bot.driver = RenderTimeoutDriver(stranded)
try:
    bot.updateStatus("/busy")
except bot.SessionExpired:
    pass
else:
    raise AssertionError("get() timeout bypassed the dead-session check")

print("ok: recovers from the auth-page redirect, leaves a healthy tab alone, "
      "raises SessionExpired when the reload bounces back to sign-in "
      "(even when the reload navigation itself times out)")
