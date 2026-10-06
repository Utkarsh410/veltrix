"""Write docs/CHANNEL_SCHEMA.md from the schema in code, so the two cannot drift.

  python tools/gen_schema_doc.py
"""

from pathlib import Path

from veltrix_engine.schema import CANONICAL_UNIT, CHANNELS, UNIT_FACTORS

HEAD = """# Channel schema

Generated from `src/veltrix_engine/schema.py` by `tools/gen_schema_doc.py`. Edit the code, not this file.

Every importer maps its source columns onto these channel ids, and every analysis
module reads and writes only these ids.

## Conventions

Vehicle axes follow ISO 8855: x forward, y left, z up.

| Quantity | Convention |
|---|---|
| Longitudinal acceleration | +ve accelerating, −ve braking |
| Lateral acceleration | +ve in left-hand corners |
| Steering | +ve steering left; degrees at the steering wheel |
| Suspension travel | +ve in compression |
| Position | metres in a flat local plane; orientation arbitrary |
| Corners | `fl`, `fr`, `rl`, `rr` — left and right as seen by the driver |
| Time base | uniform after normalisation; step channels (`lap`, `gear`) are held, never interpolated |
| Lap distance | metres from the start/finish line, scaled so every full lap has the same length |

A file whose signs differ is imported with `flip=("steering", "acc_lat", ...)`.
The engine never guesses a sign.

## Three layers

| Layer | What it is | Where |
|---|---|---|
| Raw | The file as read: source names, source units, untouched | `Session.raw` |
| Normalised | Schema ids, schema units and signs, uniform time base | `Session.channels` |
| Derived | Computed by the engine from normalised channels | `Session.derived` |

## Units accepted on import

Units are read from the file's units row. Anything converted or assumed is recorded
in `Session.notes`.

| Quantity | Stored as | Converted from |
|---|---|---|
"""


def main() -> None:
    lines = [HEAD.rstrip("\n")]
    for q, table in UNIT_FACTORS.items():
        others = ", ".join(u for u, f in table.items() if f != 1)
        lines.append(f"| {q} | {CANONICAL_UNIT[q]} | {others} |")
    lines.append("| temperature | °C | °F, K |")
    lines.append("| pedal position | % | 0–1 fraction |")
    lines += ["", "Without a units row the engine assumes the stored unit, except where the values "
              "make another unit obvious (accelerations above 4 → m/s², tyre pressures above 10 → psi, "
              "temperatures of 120–260 → °F, speed checked against distance). Each such guess is a warning.", ""]

    for kind, title in (("measured", "Measured channels"), ("derived", "Derived channels")):
        lines += [f"## {title}", ""]
        if kind == "measured":
            lines += ["| Channel | Unit | Description | Sign | Example source names |", "|---|---|---|---|---|"]
        else:
            lines += ["| Channel | Unit | Description | Sign | Computed from |", "|---|---|---|---|---|"]
        for cid, d in CHANNELS.items():
            if d.kind != kind:
                continue
            last = ", ".join(d.aliases[:3]) if kind == "measured" else ", ".join(d.requires)
            desc = d.description.replace("|", "\\|")
            sign = d.sign.replace("|", "\\|")
            lines.append(f"| `{cid}` | {d.unit} | {desc} | {sign} | {last} |")
        lines.append("")
    out = Path(__file__).resolve().parents[1] / "docs" / "CHANNEL_SCHEMA.md"
    out.write_text("\n".join(lines))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
