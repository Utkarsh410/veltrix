# User guide

How to do the main jobs in Veltrix Data. Features marked **experimental** work
in tests but have not been used on real data of that kind.

**Without Terminal:** double-click **Veltrix Data.app** in the project folder
(build it once with `sh tools/build_mac_app.sh`), or drop a telemetry file on
it. This is the desktop app this guide describes. It is not the same as
*Veltrix Report.app*, which only makes the HTML report, or the browser page
from `veltrix workspace` — neither of those has the charts or the maths editor.

```bash
.venv/bin/pip install -e ".[ui,all]"     # once
.venv/bin/veltrix app                    # the same app, from Terminal
```

Everything is in the menu bar at the top of the screen (**File, Charts, Maths,
View**). Panels on the right are tabs: At the cursor, Maths channels, Events
and notes, Track map, Video. A panel you close comes back from **View**.

## 1. Import a file

**Open…** reads a file and analyses it with the engine's own mapping. Formats:
CSV, MoTeC `.ld` (+ `.ldx`), iRacing `.ibt`, VBOX `.vbo`, AiM `.xrk`, MDF/MF4,
MAT, CAN logs with a DBC. Large files load in the background; the window stays
usable, MDF files show a progress bar, and **Cancel loading** stops it.

A file that cannot be read gives a message saying why; the session already
open is left alone.

## 2. Inspect the channels

The **Measures and laps** panel is down the left of the window. Its
**Measures** tab lists every channel as a button with its unit: a button
pressed in is drawn; click one to give the channel a chart of its own, click
again to stop drawing it. Beside each drawn channel is its value at the
cursor. **Sort channels** puts the drawn ones first, and the search box finds
a channel by name.

The **Channel details** tab lists every channel with its unit, sample rate, layer
(normalised, corrected, calculated, or raw for columns that were not mapped),
kind (measured, corrected/reconstructed, calculated) and notes such as "unit
assumed" or "recorded in coarse steps". Type in the search box to filter by
any of those. Hover a row for its description, formula and where its unit
came from. Double-click a row to add the channel to the selected chart.

## 3. Map the columns yourself

**Open with mapping…** shows every column of the file before anything is
analysed: what the engine takes it to be, what decided that (the file format, a
verified profile, or name plus values — a suggestion), how confident it is,
the unit and any problem (values that do not fit, two columns claiming one
channel).

- Change a channel in the **Channel** column; choose "not mapped" to leave a
  column out.
- Type a unit where the file gives none. A unit left "unknown" will be
  assumed, and flagged as assumed.
- Tick **Confirmed** for rows you have checked, or **Confirm all without
  problems**.
- **Save as profile…** keeps the confirmed rows for the next file from the
  same logger; **Load profile…** applies one. A saved profile that fits the
  file is applied by itself, and the line at the top says which and why. A profile rule only counts as
  verified if it was confirmed and the values fit.

For a large MDF file, a list of its channels comes first: untick what you do
not need (search, then **Untick all shown**) and only the rest is loaded.

Afterwards the **Channels** panel shows, for each channel, the column it came
**From** and how it was **Mapped by** — the file format, a profile, a name
match, your choice — and where its unit came from.

## 4. Charts and the cursor

Charts are stacked on one time axis, each with the same plotting height and the
same left edge, however many there are and whatever the window's size. What each
chart draws is named in a strip above it, in the curve's colour, rather than in a
legend over the curves. With laps loaded the strip shows each channel's line
style (the lap colours are in **Tests loaded**), and the delta chart's strip
gives every lap's final time difference to the reference. Drag to pan, scroll to zoom; **Whole
session** shows everything. Click or drag the white line to move the cursor:
it moves on every chart, on the map and in the video.

**At the cursor** shows each charted channel's value from the full-resolution
data. A value that does not fall on a logged sample is marked "≈ interpolated"
(continuous channels) or "↦ held" (gear, lap, flags). What is drawn may be
thinned for the screen; what is read out never is.

**Add chart** / **Remove chart** change the layout; click a chart to select it.

Channels in different units can share a chart: the first unit has the left
axis and each further unit gets its own axis and scale on the right, coloured
like its channel.

**Charts ▸ Mark a range** (⌘R) shades a stretch on every chart; drag its
edges. The **Marked range** tab then shows, for each charted channel, the
minimum, maximum, mean and standard deviation over the range, the values at
its two edges and the change between them — from every logged sample, not from
what is drawn. **Zoom to it** shows just that stretch; **Clear** removes it.

## 5. Load and compare laps

Laps are chosen in the **Measures and laps** panel on the left.

- **Laps tab** — every lap of the session with its time, gap to the fastest
  and status. **Click a lap to load it; click it again to unload it.** Load
  as many as you like. **Best two laps** loads the fastest lap and the next
  fastest; **Unload all** goes back to the whole session. Out-laps, in-laps
  and part laps are greyed: they did not run line to line and cannot be
  overlaid.
- **Measures tab, Tests loaded** — the session and its loaded laps: colour,
  lap number, time and difference to the reference. **X** unloads a lap (on
  the session's row, all of them). The first lap loaded is the reference;
  click another lap's text to make it the reference.

With no lap loaded the charts show the whole session against time. As soon as
a lap is loaded they show the loaded laps over each other instead, with a
delta-time chart at the bottom (positive = slower than the reference, always
worked out place for place). **Aligned by** switches between:

- **Distance** — the same x is the same place on the track. Use this to see
  where time is gained or lost.
- **Time** — the same x is the same moment of each lap, not the same place.

**Corner by corner.** With two or more laps loaded, a table under the charts
sets the reference lap against one other lap (pick it in **Against** when more
than two are loaded), one row per corner: time in the corner's zone, braking
point, brake release, minimum speed, throttle-on and exit speed — each cell
"reference → this lap" — and what differed. Braking point and release are
metres from the corner's turn-in, throttle-on metres after its apex: fixed
points, the same for every lap, so they can be found on track. "flat" means the
throttle was never lifted. Click a row to zoom the charts to that corner and
light it on the track map. Corners marked **inferred** were found in the data:
their numbers are this app's, not the circuit's (a circuit file gives the
official ones). Drag the bar above the table to share the height differently.

On the **track map**, a tick across the track marks where each corner's zone
begins, and each corner is labelled at its apex — "T5 ?" for an inferred corner.

The line above the charts always says which mode is showing. Click or drag in
a chart to move the cursor: beside each drawn measure the panel shows one
value per loaded lap, in the lap's colour, and the map, video and **At the
cursor** panel follow the reference lap.

## 6. Create a maths channel

Choose **Maths ▸ Maths channel editor…** in the menu bar (⌘M), press **Maths
channel editor** in the toolbar, or **New…** in the Maths channels tab on the
right. It opens as its own window.

1. Type a name and a formula. The formula is coloured as you type, and a name
   that no channel answers to gets a red wavy line. Pick channels from the
   **Channels** tab (double-click inserts one) and functions from **Functions**.
2. The line under the formula says what it gives — `✓ gives g · uses acc_lat,
   acc_long` — or what is wrong, with the place marked.
3. The **preview** plots the result over whatever the main charts are showing,
   before anything is saved, with how many samples are invalid.
4. **Save channel** (Ctrl+S) keeps it, computes it for the open session and
   for every later one, and lists it among the measures. **Plot on
   selected chart** adds it to a chart.

```
sqrt(acc_lat^2 + acc_long^2)                      → g
derivative(speed in m/s) in g                     → g
if(throttle > $limit, 1, 0)                       → uses the parameter "limit"
(RearWheelSpeed - speed) / speed * 100            → uses another maths channel
```

**Undo last change** takes back the last save, delete, rename or parameter
change (typing in the formula has its own ⌘Z). Thresholds go in the
**Parameters** tab and are used as `$name`; changing one
recomputes every channel that uses it. The **Dependencies** tab shows what a
channel is built from, what is built on it, and how the saved version was
calculated. Tick "an estimate" for anything that is not a measurement. A
channel cannot be renamed or deleted while another uses it. The full language
is in [maths-channels.md](maths-channels.md).

## 7. Track map

The map follows the cursor and can be coloured by any channel for the lap the
cursor is in. Click the map to move the cursor to that place on the current
lap. Your timed notes are marked on it as amber diamonds; hover one to read
it. Under it the app says where the shape came from: "Logged position", or
"Rebuilt from speed and lateral g … not surveyed positions". If a session has
neither, there is no map, and the panel says so.

## 8. Events and notes

**Events and notes** lists what the detectors found, each worded as a
candidate ("Possible wheelspin — requires review"), with lap, severity and
position. Click a row to jump there. Above the list the app says which rules
ran and which could not ("Not looked for: wheelspin (…no channel named
wheel_speed_fl)").

**Events ▸ Event rules…** (⌘E) shows the rules as a table: what starts an
event, what ends it, how long it must last, how it is graded, and the
thresholds they share. **Try on this session** counts what each rule finds
with the values on screen; **Fit thresholds to this session** raises any
threshold that sits inside this session's own sensor noise; **Save** keeps the
rules and the events list is rebuilt. Thresholds are fitted to each session
automatically when it is opened — the line above the list says what was
raised and why — but they are still not validated against a car.

Your own notes are separate: type one and press **Add note at cursor**. Notes
are drawn as dashed lines on the charts and are saved with the file, so they
are back the next time the same file is opened.


**Reviewing an event.** Select an event in **Events and notes** and press
**Confirm** (it happened — you checked the video, the driver, the data),
**Reject** (a false detection) or **Note…**; **Clear review** takes your verdict
off. Each event shows what it was found with (the rule's thresholds, and which
were fitted to this session) and its confidence; the box underneath shows the
evidence, your review and the status as first detected. Reviews are saved per
file and come back when it is opened again, and the report built from the app's
data shows them. If the rules or thresholds change so that an event is no longer
found, its review is kept, with the event as it was originally detected.

## 9. Video — experimental

**Add video…** in the Video panel links a recording to the session.

- **Offset**: telemetry time = video time + offset. A positive offset means
  the video started after the log did.
- Better: pause the video on something you can see in the data (the brake
  going on, a gear change, the start line), put the cursor on the same event,
  type what it is and press **Anchor: this frame is the cursor**. With two or
  more anchors the offset is fitted and the table shows how far each anchor is
  from the fit.
- The status line says how well the sync is known. A manual offset or a single
  anchor is "not validated". Frame rate is reported separately: it limits how
  finely a moment can be picked, not how good the sync is.
- **Play** moves the cursor with the video; moving the cursor seeks the video.
- Add more cameras with **Add video…**: they are shown side by side, each on
  its own sync. The one selected in the list drives the cursor and owns the
  offset and anchors shown.
- **Piecewise** follows the anchors segment by segment instead of one straight
  fit, for a recording that dropped frames or was joined from clips. It needs
  three or more anchors, and reports how well the anchors predict each other.
- If the file has moved, the panel says so; **Relink…** points at it and keeps
  the sync.

Tested only with a short clip the test suite encodes itself.

## 10. Workspaces

**Save workspace…** stores the charts, their channels, the view, the cursor
and the loaded laps. **Open workspace…** brings them back, on this
session or another. Channels a session does not have are listed, and the rest
of the layout still opens.

## 11. Reports

```bash
.venv/bin/veltrix report path/to/session.csv -o session.html
.venv/bin/veltrix engineer path/to/session.csv --markdown report.md
```

**File ▸ Make report…** (⌘P) in the app writes the open session's report and
opens it. Either way the report includes your maths channels, the events found
with your rules, and your notes for that file (`--no-app-data` on the command
line leaves them out).

The HTML report opens in a browser. Its tabs: **Engineering report** (nine
sections; each finding with evidence, calculation, confidence and
recommendation — setup items are hypotheses with a suggested investigation,
and a setup change is only suggested when the evidence is better than thin);
**What to work on**; **Corner by corner**; **Incidents**; **Stints, sectors
and grip**; **Data, events and limits** (the channel mapping with where each
unit came from, maths channels, detected events, your notes, and the
limitations of the report); **Import notes**.


Braking points in the engineering report are given the way a driver reads them on track: metres
**before the corner** (its turn-in), the distance the marker boards count down to — "117 m before the
corner" — or "into the corner" when the brakes go on after the car has started to turn. Turn-in is one
fixed point per corner, taken from a typical lap.

## The car library

Several figures need the car's own numbers — wheelbase, steering ratio, mass,
weight distribution, wheel rates. Enter them once per car:

    veltrix car set "Toyota GR86" --wheelbase 2.575 --steer-ratio 13.5 --alias GR86
    veltrix car show "Toyota GR86"
    veltrix car list

Every session whose file names that car (or an alias) then uses them, in the app
and in reports. A value given when importing, or read from the file itself (an
iRacing setup gives the mass and the static front weight), comes first; the
library fills in the rest; a value known from neither is a placeholder, and the
report says so rather than quoting it as the car's. The import's notes say
which values came from where.

## When something goes wrong

A file that cannot be read gives a message naming the file and what is wrong
with it. If the app hits an error it did not expect, it saves a crash report in
its data folder (`crash_reports/`) and tells you where; it holds the error and
the versions, no telemetry, and nothing is sent anywhere. What the app did —
files read, how long it took, errors — is logged in `logs/veltrix.log` in the
same folder (on a Mac: `~/Library/Application Support/Veltrix Data`).

A file saved by a newer release of Veltrix (a workspace, notes, reviews, a
project) is refused with a message rather than read in part, and is never
overwritten.

## What is not there yet

A real, signed app bundle (the launchers are tied to this folder).
