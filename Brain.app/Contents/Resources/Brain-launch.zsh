#!/bin/zsh
#
# Brain launcher (bundled resource).
#
# Double-clicking Brain.app opens the local Brain web UI. The bundle's
# Finder entry point is a native AppleScript applet
# (Contents/MacOS/applet) that invokes this script. If a healthy server is
# already answering on the fixed local health endpoint it is simply
# opened; otherwise the project's existing `.venv/bin/brain serve` is
# started in the background (no Terminal window) and the URL is opened
# once that health endpoint succeeds.
#
# The project root is derived from the bundle's own location, so the
# launcher works as long as the bundle stays in the project root. Nothing
# about the user's account is hardcoded here.
#
#   <project-root>/Brain.app/Contents/Resources/Brain-launch.zsh

set -u

# --- Fixed local values -----------------------------------------------------
readonly BRAIN_URL="http://127.0.0.1:8787/"
readonly HEALTH_URL="http://127.0.0.1:8787/health/"
readonly OPEN_BIN="/usr/bin/open"
readonly CURL_BIN="/usr/bin/curl"
readonly OSASCRIPT_BIN="/usr/bin/osascript"
readonly NOHUP_BIN="/usr/bin/nohup"
readonly SLEEP_BIN="/bin/sleep"

# Resolve the project root from the bundle path:
#   <project-root>/Brain.app/Contents/Resources/Brain-launch.zsh
SCRIPT_DIR="${0:A:h}"                # .../Brain.app/Contents/Resources
PROJECT_ROOT="${SCRIPT_DIR:h:h:h}"   # .../<project-root>
readonly PROJECT_ROOT

readonly BRAIN_BIN="${PROJECT_ROOT}/.venv/bin/brain"
readonly LOG_DIR="${PROJECT_ROOT}/data/temp"
readonly LOG_FILE="${LOG_DIR}/brain-app.log"

# Bounded startup wait (seconds).
readonly STARTUP_TIMEOUT=45

# --- Helpers ----------------------------------------------------------------
notify() {
  # One concise, actionable dialog. It never contains secrets, file
  # contents, or the user's account paths.
  "$OSASCRIPT_BIN" -e "display dialog \"$1\" with title \"Brain\" buttons {\"OK\"} default button \"OK\" with icon caution" >/dev/null 2>&1
}

server_is_up() {
  # Brain's fixed health endpoint must answer with HTTP success: 200
  # (ok/degraded) means the app is usable, while 503 (unhealthy) and
  # connection failures mean it is not. `--fail` turns any non-2xx
  # response into a nonzero exit.
  "$CURL_BIN" --fail -sS -o /dev/null --max-time 2 "$HEALTH_URL" >/dev/null 2>&1
}

open_url() {
  "$OPEN_BIN" "$BRAIN_URL"
}

# --- Already running? -------------------------------------------------------
if server_is_up; then
  open_url
  exit 0
fi

# --- Prerequisite check -----------------------------------------------------
if [[ ! -x "$BRAIN_BIN" ]]; then
  notify "Brain could not start because the local Python environment is missing. Run 'uv sync' in the project folder, then double-click Brain again."
  exit 1
fi

# --- Launch the server without a Terminal window ----------------------------
mkdir -p "$LOG_DIR" 2>/dev/null
if [[ ! -d "$LOG_DIR" ]]; then
  notify "Brain could not create its temporary log folder (data/temp). Check that the project folder is writable, then try again."
  exit 1
fi

"$NOHUP_BIN" "$BRAIN_BIN" serve >>"$LOG_FILE" 2>&1 </dev/null &
SERVER_PID=$!

# --- Wait, boundedly, for the health endpoint -------------------------------
deadline=$(( SECONDS + STARTUP_TIMEOUT ))
while (( SECONDS < deadline )); do
  if server_is_up; then
    open_url
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    # The server process exited before the URL became reachable. Open
    # the page only if something else is nonetheless answering (for
    # example a server that bound the port between our first check and
    # the launch); otherwise report the failure.
    if server_is_up; then
      open_url
      exit 0
    fi
    break
  fi
  "$SLEEP_BIN" 1
done

notify "Brain did not respond at ${BRAIN_URL} in time. Check that 'uv sync' has been run and that config/config.yaml exists, then try 'uv run brain serve' in Terminal. Startup details are in data/temp/brain-app.log."
exit 1
