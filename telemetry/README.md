# Telemetry tools

Python tools for iRacing `.ibt` telemetry files.

- `ibt.py`: minimal `.ibt` reader. It reads the header, the session-info YAML and every channel into numpy arrays.
- `analyse.py`: splits the session into laps, measures lap and sector times, and resamples every lap onto a 2 m distance grid.
  Run it as `python analyse.py <file.ibt> laps.json`.
- `corners.py`: reports braking point, minimum speed, throttle pickup and exit speed for each Spa corner on every lap.
  Run it as `python corners.py laps.json corners.json`.

Requires `numpy` and `pyyaml`.

`reports/gr86-spa-debrief.html` is the debrief generated from the 3 Oct 2026 GR86 race at Spa.
