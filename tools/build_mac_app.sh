#!/bin/sh
# Build "Veltrix Report.app": double-click it (or drop telemetry CSVs on it) to
# build a session report and open it — no Terminal needed.
#
#   sh tools/build_mac_app.sh
#
# The app is a thin launcher around `veltrix report`. It remembers where this
# folder is, so re-run this script if the folder is moved or renamed.
set -e
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ENGINE_DIR/Veltrix Report.app"
SRC="$(mktemp -t veltrix_report).applescript"

cat > "$SRC" <<APPLESCRIPT
property engineDir : "$ENGINE_DIR"

on run
	try
		set picked to choose file with prompt "Choose telemetry CSV files to analyse" with multiple selections allowed
	on error number -128
		return -- cancelled
	end try
	buildReports(picked)
end run

on open droppedFiles
	buildReports(droppedFiles)
end open

on buildReports(theFiles)
	set tool to engineDir & "/.venv/bin/veltrix"
	try
		do shell script "test -x " & quoted form of tool
	on error
		display dialog "The Veltrix engine was not found at:" & return & engineDir & return & return & "If the folder was moved (or its drive is not connected), reconnect it or run tools/build_mac_app.sh again." buttons {"OK"} default button 1 with icon stop with title "Veltrix Report"
		return
	end try
	repeat with f in theFiles
		set src to POSIX path of f
		set shortName to do shell script "basename " & quoted form of src
		display notification "Analysing " & shortName & "…" with title "Veltrix Report"
		try
			set outFile to do shell script "mkdir -p " & quoted form of (engineDir & "/reports") & " && n=\$(basename " & quoted form of src & " | sed -e 's/\\\\.gz\$//' -e 's/\\\\.csv\$//') && echo " & quoted form of (engineDir & "/reports/") & "\"\$n\"_report.html"
			do shell script quoted form of tool & " report " & quoted form of src & " -o " & quoted form of outFile & " 2>&1"
			do shell script "open " & quoted form of outFile
		on error msg
			display dialog "Could not build a report for " & shortName & ":" & return & return & msg buttons {"OK"} default button 1 with icon caution with title "Veltrix Report"
		end try
	end repeat
end buildReports
APPLESCRIPT

rm -rf "$APP"
osacompile -o "$APP" "$SRC"
rm -f "$SRC"
echo "Built: $APP"
echo "Drag it to the Dock or the Desktop. Double-click to pick files, or drop CSVs on it."
