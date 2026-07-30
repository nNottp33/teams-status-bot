#!/usr/bin/env python3
"""Guards the behaviour that let the bot lie for weeks: a stale session makes
Teams report presence "Unknown" and drop every write, so a click that doesn't
raise must NOT count as success, must NOT be retried, and must NOT be swallowed
by the loop the way a transient renderer flake is.
Run: python test_session_expired.py
"""
import importlib.util

spec = importlib.util.spec_from_file_location("bot", "teams-status-bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

bot.log = lambda *_: None          # keep the real bot.log file clean
bot.time.sleep = lambda *_: None   # no 2s repaints, no 5-minute waits


class FakeEl:
    def __init__(self, label=""):
        self._label = label

    def get_attribute(self, _):
        return self._label

    def send_keys(self, *_):
        pass


class FakeDriver:
    def __init__(self, label=""):
        self._label = label

    def implicitly_wait(self, _):
        pass

    def find_element(self, *_):
        return FakeEl()

    def find_elements(self, *_):
        return [FakeEl(self._label)]


# currentPresence reads the avatar's aria-label
bot.driver = FakeDriver("Your profile, status Available")
assert bot.currentPresence() == "Your profile, status Available"
bot.driver = FakeDriver()
bot.driver.find_elements = lambda *_: []
assert bot.currentPresence() == ""

# a stale session ("Unknown") is fatal and is NOT retried
bot.driver = FakeDriver("Your profile, status Unknown ")
bot.isLoggedIn = lambda: True
tries = []
bot.openStatusMenu = lambda s, t: (tries.append(s), (_ for _ in ()).throw(
    bot.SessionExpired("presence Unknown")))
dumped = []
bot.dumpFailureState = lambda tag: dumped.append(tag)
try:
    bot.updateStatus("/available")
except bot.SessionExpired:
    pass
else:
    raise AssertionError("updateStatus swallowed SessionExpired")
assert len(tries) == 1, f"retried a stale session: {tries}"
assert not dumped, "wrote a failure screenshot for a stale session"

# keepUpdating stops on it instead of burning the whole window
calls = []
bot.updateStatus = lambda s: (calls.append(s), (_ for _ in ()).throw(
    bot.SessionExpired("dead")))
try:
    bot.keepUpdating("/busy", every=5, hours=1)
except bot.SessionExpired:
    pass
else:
    raise AssertionError("keepUpdating swallowed SessionExpired")
assert len(calls) == 1, calls

# but a transient failure is still retried next cycle
flakes = []
bot.updateStatus = lambda s: (flakes.append(s), (_ for _ in ()).throw(
    RuntimeError("slow renderer")))
bot.keepUpdating("/busy", every=5, hours=1 / 6.)  # 2 cycles
assert len(flakes) == 2, flakes

print("ok")
