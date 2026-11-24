# Sourced by the .command scripts: closes the Terminal window a double-click
# opened once the script succeeds. A failure keeps the window, with its error.

# A double-click gives the script a login shell that has only just started; a
# script run by hand in a Terminal window has an older one, and keeps it open.
case ${TERM_PROGRAM:-}:$(ps -o etime= -p "$PPID" | tr -d ' ') in
    Apple_Terminal:00:0[0-4])
        window_tty=$(tty)
        ;;
    *)
        window_tty=
        ;;
esac

close_window() {
    if [ "$1" -ne 0 ] || [ -z "$window_tty" ]; then
        return 0
    fi
    # Runs after this script and its shell have exited; a window with other
    # tabs stays open.
    nohup sh -c 'sleep 1; exec osascript -e "$1" "$2"' sh '
        on run {windowTty}
            tell application "Terminal"
                repeat with candidate in windows
                    if (count of tabs of candidate) is 1 and tty of tab 1 of candidate is windowTty then
                        close candidate
                        exit repeat
                    end if
                end repeat
            end tell
        end run' "$window_tty" > /dev/null 2>&1 < /dev/null &
}

trap 'close_window $?' EXIT
