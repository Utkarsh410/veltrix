#!/bin/sh
# Build "Veltrix Data.app", the one app for the desktop workspace and for reports
# — no Terminal needed.
#
#   sh tools/build_mac_app.sh
#
#   double-click             opens the desktop workspace (charts, laps, maths, map, events, video)
#   drop one file on it      opens that session in the workspace
#   drop several files       writes a report of each, with its debrief (HTML and PDF) and coaching
#                            cards, into reports/ in this folder, and opens an index page linking them
#
# In the workspace, File ▸ Make report… and File ▸ Make reports for files… do the same.
#
# The app is a thin launcher around `veltrix app` and `veltrix reports`. It remembers where this
# folder is, so re-run this script if the folder is moved or renamed.
set -e
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DESK="$ENGINE_DIR/Veltrix Data.app"
OLD="$ENGINE_DIR/Veltrix Report.app"         # merged into Veltrix Data.app
SRC="$(mktemp -t veltrix_data).applescript"

cat > "$SRC" <<APPLESCRIPT
property engineDir : "$ENGINE_DIR"

on run
	if engineFound() then do shell script quoted form of (engineDir & "/.venv/bin/veltrix") & " app > /dev/null 2>&1 &"
end run

on open droppedFiles
	if not engineFound() then return
	set tool to engineDir & "/.venv/bin/veltrix"
	if (count of droppedFiles) is 1 then
		do shell script quoted form of tool & " app " & quoted form of POSIX path of (item 1 of droppedFiles) & " > /dev/null 2>&1 &"
		return
	end if
	set args to ""
	repeat with f in droppedFiles
		set args to args & " " & quoted form of POSIX path of f
	end repeat
	display notification "Making " & (count of droppedFiles) & " reports…" with title "Veltrix Data"
	try
		do shell script quoted form of tool & " reports" & args & " -o " & quoted form of (engineDir & "/reports") & " --open 2>&1"
	on error msg
		display dialog "Some files could not be read; the index page lists why." & return & return & msg buttons {"OK"} default button 1 with icon caution with title "Veltrix Data"
	end try
end open

on engineFound()
	try
		do shell script "test -x " & quoted form of (engineDir & "/.venv/bin/veltrix")
		return true
	on error
		display dialog "The Veltrix engine was not found at:" & return & engineDir & return & return & "If the folder was moved (or its drive is not connected), reconnect it or run tools/build_mac_app.sh again." buttons {"OK"} default button 1 with icon stop with title "Veltrix Data"
		return false
	end try
end engineFound
APPLESCRIPT

rm -rf "$DESK"
osacompile -o "$DESK" "$SRC"
rm -f "$SRC"
echo "Built: $DESK"
if [ -d "$OLD" ]; then
	rm -rf "$OLD"
	echo "Removed Veltrix Report.app: Veltrix Data.app makes reports now (drop several files on it)."
	echo "If it was in the Dock, take it out and put Veltrix Data.app there instead."
fi
echo "Double-click to open the workspace; drop one file to open it, several to make their reports."
