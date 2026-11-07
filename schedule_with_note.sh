#!/bin/bash
# Dynamic scheduler with note for next check
# Usage: ./schedule_with_note.sh <minutes> "<note>" [target_window]

MINUTES=${1:-3}
NOTE=${2:-"Standard check-in"}
TARGET=${3:-"tmux-orc:0"}
DIR="$(cd "$(dirname "$0")" && pwd)"
NOTE_FILE="$DIR/next_check_note.txt"

# Resolves session, window and (if given) pane, so session:99 or a wrong window name fails here
# rather than when the check is due
if ! tmux list-panes -t "$TARGET" >/dev/null 2>&1; then
    echo "Target $TARGET does not exist. Pass your own window: \$(tmux display-message -p '#{session_name}:#{window_index}')"
    exit 1
fi

# Create a note file for the next check
echo "=== Next Check Note ($(date)) ===" > "$NOTE_FILE"
echo "Scheduled for: $MINUTES minutes" >> "$NOTE_FILE"
echo "" >> "$NOTE_FILE"
echo "$NOTE" >> "$NOTE_FILE"

echo "Scheduling check in $MINUTES minutes with note: $NOTE"

# Calculate the exact time when the check will run
CURRENT_TIME=$(date +"%H:%M:%S")
RUN_TIME=$(date -v +${MINUTES}M +"%H:%M:%S" 2>/dev/null || date -d "+${MINUTES} minutes" +"%H:%M:%S" 2>/dev/null)

# Use nohup to completely detach the sleep process
SECONDS_TO_WAIT=$(echo "$MINUTES * 60" | bc)
MESSAGE="Time for orchestrator check! cat $(printf %q "$NOTE_FILE")"
nohup bash -c "sleep $SECONDS_TO_WAIT && tmux send-keys -t $(printf %q "$TARGET") -l $(printf %q "$MESSAGE") && sleep 1 && tmux send-keys -t $(printf %q "$TARGET") Enter" > /dev/null 2>&1 &

# Get the PID of the background process
SCHEDULE_PID=$!

echo "Scheduled successfully - process detached (PID: $SCHEDULE_PID)"
echo "SCHEDULED TO RUN AT: $RUN_TIME (in $MINUTES minutes from $CURRENT_TIME)"
