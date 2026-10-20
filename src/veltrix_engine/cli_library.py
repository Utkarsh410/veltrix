"""
`veltrix library`: the session library from the command line (library.py).

  veltrix library scan ~/Telemetry                    index every session under a folder
  veltrix library list --track Spa --since 2026-09-01
  veltrix library list --search wet --tag baseline
  veltrix library show 12
  veltrix library alias track "Circuit de Spa-Francorchamps Grand Prix" Spa
  veltrix library set 12 driver "A. Driver"
  veltrix library tag 12 13 --add wet baseline
  veltrix library event "Spa test day" --add 12 13 --kind "test day"
  veltrix library events --suggest                    (--create makes them)
  veltrix library progress --track Spa --car GR86
  veltrix library best
  veltrix library compare 12 13 --change "rear bar +1"
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

LIST_COLUMNS = ["id", "date", "track", "car", "driver", "simulator", "event", "tags", "laps", "best_lap_s",
                "ideal_lap_s", "file"]


def _time(seconds) -> str:
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)) or pd.isna(seconds):
        return "—"
    m, s = divmod(float(seconds), 60.0)
    return f"{int(m)}:{s:06.3f}"


def _print_table(table: pd.DataFrame, columns: list) -> None:
    if table.empty:
        print("No sessions match.")
        return
    shown = table[[c for c in columns if c in table.columns]].astype(object)
    for c in shown.columns:
        if c.endswith("_lap_s") or c == "best_so_far_s":
            shown[c] = shown[c].map(_time)
        elif c == "date":
            shown[c] = shown[c].fillna("").str[:16]
        elif c in ("change_s", "consistency_s"):
            shown[c] = shown[c].map(lambda v: "" if pd.isna(v) else f"{v:+.3f}" if c == "change_s" else f"{v:.3f}")
    shown = shown.where(shown.notna(), "").astype(str)
    print(shown.to_string(index=False, max_colwidth=36))


def add_filters(p) -> None:
    p.add_argument("--search", help="text in the file name, track, car, driver, event, tags or note")
    p.add_argument("--track")
    p.add_argument("--car")
    p.add_argument("--driver")
    p.add_argument("--sim", dest="simulator", help="simulator, e.g. iRacing")
    p.add_argument("--tag", nargs="*", help="sessions with all of these tags")
    p.add_argument("--event")
    p.add_argument("--type", dest="session_type", help="session type from the file (Practice, Race…)")
    p.add_argument("--since", help="from this date (YYYY-MM-DD)")
    p.add_argument("--until", help="up to this date")


def _filters(args) -> dict:
    return {"text": args.search, "track": args.track, "car": args.car, "driver": args.driver,
            "simulator": args.simulator, "tag": args.tag or None, "event": args.event,
            "session_type": args.session_type, "since": args.since, "until": args.until}


def add_parser(sub) -> None:
    lib = sub.add_parser("library", help="the session library: index folders, then find, filter, tag, group and "
                                         "compare sessions without opening them")
    lib.add_argument("--library", metavar="FILE", help="the library file (default: library.sqlite in the app's data folder)")
    ls = lib.add_subparsers(dest="library_command", required=True)

    p = ls.add_parser("scan", help="index the telemetry files under folders (new and changed ones are read)")
    p.add_argument("folders", nargs="+")
    p.add_argument("--jobs", type=int, default=1, help="files read side by side")
    p.add_argument("--rescan", action="store_true", help="read every file again, changed or not")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--no-app-data", action="store_true", help="ignore the app's car library")

    p = ls.add_parser("list", help="sessions, filtered")
    add_filters(p)
    p.add_argument("--all", action="store_true", help="also files that failed, are missing or are duplicates")
    p.add_argument("--sort", default="date", help="column to sort by (default: date)")
    p.add_argument("--desc", action="store_true", help="sort from the highest down")
    p.add_argument("--csv", metavar="FILE", help="also write the list as CSV")

    p = ls.add_parser("show", help="everything the library holds about one session")
    p.add_argument("id", type=int)

    p = ls.add_parser("set", help="correct what the file says: track, car, driver, simulator, date, session_type")
    p.add_argument("id", type=int)
    p.add_argument("field")
    p.add_argument("value", help='the value; "" puts back what the file says')

    p = ls.add_parser("alias", help="one name for what files spell differently (track, car or driver)")
    p.add_argument("kind", choices=("track", "car", "driver"))
    p.add_argument("alias", help="the name as the files give it")
    p.add_argument("name", nargs="?", help="the name to use; left out, the alias is removed")

    ls.add_parser("aliases", help="list the aliases")

    p = ls.add_parser("tag", help="add or remove tags")
    p.add_argument("ids", nargs="+", type=int)
    p.add_argument("--add", nargs="*", default=[])
    p.add_argument("--remove", nargs="*", default=[])

    p = ls.add_parser("note", help="a note on a session")
    p.add_argument("id", type=int)
    p.add_argument("text")

    p = ls.add_parser("event", help="create an event (race weekend, test day) and put sessions in it")
    p.add_argument("name")
    p.add_argument("--add", nargs="*", type=int, default=[], help="session ids to put in it")
    p.add_argument("--leave", nargs="*", type=int, default=[], help="session ids to take out of their event")
    p.add_argument("--kind", default="", help='e.g. "race weekend", "test day"')
    p.add_argument("--note", default="")
    p.add_argument("--delete", action="store_true", help="delete the event (its sessions stay)")

    p = ls.add_parser("events", help="list events; --suggest proposes them from track and date")
    p.add_argument("--suggest", action="store_true")
    p.add_argument("--create", action="store_true", help="with --suggest: create the events proposed")

    p = ls.add_parser("progress", help="best and ideal lap session by session, for one track and car")
    add_filters(p)

    p = ls.add_parser("best", help="the fastest session for each track and car")
    add_filters(p)

    p = ls.add_parser("compare", help="session B against session A, by library id (did the change work?)")
    p.add_argument("a", type=int)
    p.add_argument("b", type=int)
    p.add_argument("--change", default="", help="what was changed, when the files do not record it")
    p.add_argument("--markdown", metavar="FILE", help="also write the comparison as Markdown")

    p = ls.add_parser("forget", help="take sessions out of the library (the files are not touched)")
    p.add_argument("ids", nargs="+", type=int)

    lib.set_defaults(func=run)


def run(args) -> int:
    from .library import Library
    with Library(args.library) as lib:
        return COMMANDS[args.library_command](lib, args)


def _scan(lib, args) -> int:
    cars_dir = None
    if not args.no_app_data:
        from .user_data import data_dir
        cars_dir = data_dir() / "cars"

    def progress(done, total, path, kind, summary):
        what = summary.get("error") if kind == "failed" else (
            f"{summary.get('venue') or '?'}, {summary.get('laps')} laps, best {_time(summary.get('best_lap_s'))}"
            if kind != "duplicate" else "duplicate")
        print(f"  [{done}/{total}] {path.name}: {kind} — {what}")
    counts = lib.scan(args.folders, recursive=not args.no_recursive, jobs=args.jobs, progress=progress,
                      cars_dir=cars_dir, rescan=args.rescan)
    print(", ".join(f"{n} {k}" for k, n in counts.items() if n) or "Nothing to index.")
    print(f"Library: {lib.path}")
    return 0


def _list(lib, args) -> int:
    include = ("ok", "failed", "missing", "duplicate") if args.all else ("ok",)
    table = lib.find(include=include, sort=("-" if args.desc else "") + args.sort, **_filters(args))
    _print_table(table, LIST_COLUMNS + (["status"] if args.all else []))
    if len(table):
        print(f"\n{len(table)} session(s)")
    if args.csv:
        table.to_csv(args.csv, index=False)
        print(f"Written to {args.csv}")
    return 0


def _show(lib, args) -> int:
    row = lib.get(args.id)
    for k, v in row.items():
        if v is not None and v != "":
            print(f"  {k:<15} {_time(v) if k.endswith('_lap_s') else v}")
    return 0


def _set(lib, args) -> int:
    lib.set(args.id, args.field, args.value or None)
    print(f"Session {args.id}: {args.field} " + (f"= {args.value}" if args.value else "as the file says"))
    return 0


def _alias(lib, args) -> int:
    lib.alias(args.kind, args.alias, args.name)
    print(f"{args.kind} '{args.alias}' → {args.name}" if args.name else f"Alias '{args.alias}' removed.")
    return 0


def _aliases(lib, args) -> int:
    table = lib.aliases()
    print(table.to_string(index=False) if len(table) else "No aliases.")
    return 0


def _tag(lib, args) -> int:
    if args.add:
        lib.tag(args.ids, *args.add)
    if args.remove:
        lib.tag(args.ids, *args.remove, remove=True)
    for sid in args.ids:
        print(f"  {sid}: {lib.get(sid)['tags'] or '(no tags)'}")
    return 0


def _note(lib, args) -> int:
    lib.note(args.id, args.text)
    return 0


def _event(lib, args) -> int:
    if args.delete:
        lib.delete_event(args.name)
        print(f"Event '{args.name}' deleted; its sessions are in no event.")
        return 0
    lib.create_event(args.name, kind=args.kind, sessions=args.add, note=args.note)
    if args.leave:
        lib.leave_event(args.leave)
    _print_table(lib.find(event=args.name), LIST_COLUMNS)
    return 0


def _events(lib, args) -> int:
    if args.suggest:
        proposals = lib.suggest_events()
        if not proposals:
            print("Every session with a track is in an event already.")
        for p in proposals:
            if args.create:
                lib.create_event(p["name"], sessions=p["sessions"])
            print(f"  {'created' if args.create else 'proposed'}: {p['name']} — sessions "
                  + ", ".join(map(str, p["sessions"])))
        return 0
    table = lib.events()
    if table.empty:
        print("No events. veltrix library events --suggest proposes them from track and date.")
        return 0
    table["best_lap_s"] = table["best_lap_s"].map(_time)
    print(table.to_string(index=False))
    return 0


def _progress(lib, args) -> int:
    table = lib.progress(**_filters(args))
    if "warning" in table.attrs:
        print(f"Note: {table.attrs['warning']}\n")
    _print_table(table, ["id", "date", "track", "car", "driver", "event", "flying_laps", "best_lap_s", "change_s",
                         "best_so_far_s", "ideal_lap_s", "median_lap_s", "consistency_s", "file"])
    return 0


def _best(lib, args) -> int:
    _print_table(lib.best(**_filters(args)), ["track", "car", "best_lap_s", "ideal_lap_s", "id", "date", "driver", "file"])
    return 0


def _compare(lib, args) -> int:
    from .analysis import ab
    result = lib.compare(args.a, args.b, args.change)
    a, b = result["library"]["a"], result["library"]["b"]
    title = f"Session {b['id']} ({b['date'][:10]}) against session {a['id']} ({a['date'][:10]})"
    text = ab.to_markdown(result, title)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
        print(f"Written to {args.markdown}")
    return 0 if result["comparable"] else 2


def _forget(lib, args) -> int:
    for sid in args.ids:
        lib.forget(sid)
    print(f"{len(args.ids)} session(s) taken out of the library; the files are where they were.")
    return 0


COMMANDS = {"scan": _scan, "list": _list, "show": _show, "set": _set, "alias": _alias, "aliases": _aliases,
            "tag": _tag, "note": _note, "event": _event, "events": _events, "progress": _progress, "best": _best,
            "compare": _compare, "forget": _forget}
