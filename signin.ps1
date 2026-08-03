# One-time interactive Teams sign-in, fully inside Docker (no host Python).
# The Chrome window shows up on your desktop via WSLg (Win11 + Docker Desktop).
#
# 1. Run:  .\signin.ps1
# 2. Sign in in the Chrome window (MFA/OTP).
# 3. Wait for "[login] Signed in. Session saved to ./chrome-profile."
# 4. Press Ctrl+C, then run:  docker compose up -d
docker compose stop
docker compose run --rm --no-deps `
  -e HEADLESS=0 -e DISPLAY=:0 `
  -v /run/desktop/mnt/host/wslg/.X11-unix:/tmp/.X11-unix `
  --entrypoint python teams-status-bot teams-status-bot.py
