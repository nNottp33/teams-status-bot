#!/bin/sh
# Runs the bot on weekdays (Mon-Fri) between 08:00 and 18:00 container time
# (TZ=Asia/Bangkok is set in the Dockerfile). Outside the window it just waits.
# ponytail: minute-poll loop instead of cron — no env-propagation headache.
echo "[scheduler] Started. Now: $(date). Window: Mon-Fri 08:00-18:00 $TZ"
waiting_logged=0
while true; do
    dow=$(date +%u)
    h=$(date +%H)
    m=$(date +%M)
    # strip leading zero (busybox date has no %-H)
    h=${h#0}
    m=${m#0}
    if [ "$dow" -le 5 ] && [ "$h" -ge 8 ] && [ "$h" -lt 18 ]; then
        # run until 18:00, even if the container started mid-day
        FOR_HOURS=$(awk "BEGIN{printf \"%.2f\", 18 - $h - $m / 60}")
        export FOR_HOURS
        echo "[scheduler] In work window, starting bot for ${FOR_HOURS}h"
        python teams-status-bot.py
        echo "[scheduler] Bot exited, waiting for next window"
        waiting_logged=0
    elif [ "$waiting_logged" -eq 0 ]; then
        echo "[scheduler] Outside work window ($(date)), sleeping until next weekday 08:00"
        waiting_logged=1
    fi
    sleep 60
done
