#!/usr/bin/env sh
set -eu

SCRIPT="/script/immich_auto_album.sh"

# source util library
. util.sh

# Set defaults ---
CRON_EXPRESSION="${CRON_EXPRESSION:-}"

# RUN_IMMEDIATELY depends on CRON set
if [ ! -z "$CRON_EXPRESSION" ]; then
    RUN_IMMEDIATELY="${RUN_IMMEDIATELY:-false}"
else
    RUN_IMMEDIATELY="${RUN_IMMEDIATELY:-true}"
fi

# Perform immediate run, under the same lock as the cron runs so two runs never overlap
LOCK="$(dirname "${STATE_FILE:-/script/state/organizer-state.json}")/run.lock"
mkdir -p "$(dirname "$LOCK")"
if is_true "$RUN_IMMEDIATELY"; then
    flock "$LOCK" "$SCRIPT" > /proc/1/fd/1 2>/proc/1/fd/2
fi

# Set up SuperCronic schedule
if [ -n "$CRON_EXPRESSION" ]; then
    CRONTAB_PATH="/tmp/crontab"
   
    # Create and lock down crontab
    touch "$CRONTAB_PATH"
    chmod 0600 "$CRONTAB_PATH"

    # populate crontab
    echo "$CRON_EXPRESSION UNATTENDED=1 /script/immich_auto_album.sh > /proc/1/fd/1 2>/proc/1/fd/2" > "$CRONTAB_PATH"
    # FULL_RUN_CRON adds a daily full run next to incremental runs on CRON_EXPRESSION. The lock keeps an
    # incremental run from starting while the full run is still going (it is skipped, not queued).
    if [ -n "${FULL_RUN_CRON:-}" ]; then
        echo "$CRON_EXPRESSION UNATTENDED=1 flock -n \"$LOCK\" /script/immich_auto_album.sh > /proc/1/fd/1 2>/proc/1/fd/2" > "$CRONTAB_PATH"
        echo "$FULL_RUN_CRON UNATTENDED=1 INCREMENTAL=0 flock \"$LOCK\" /script/immich_auto_album.sh > /proc/1/fd/1 2>/proc/1/fd/2" >> "$CRONTAB_PATH"
    fi
    
    # SuperCronic debug logging
    DEBUG_PARM=""
    if [ "${LOG_LEVEL:-}" = "DEBUG" ]; then
        DEBUG_PARM="-debug"
    fi
    /usr/local/bin/supercronic -passthrough-logs -no-reap -split-logs $DEBUG_PARM $CRONTAB_PATH
fi