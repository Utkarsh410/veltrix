"""
Freeze what each real session gives now (tests/real_expected/, kept on this
machine), and print what changed against the previous freeze.

    .venv/bin/python tools/freeze_real_sessions.py            # every session found
    .venv/bin/python tools/freeze_real_sessions.py spa_gr86_ibt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from real_sessions import CORPUS, EXPECTED, find, summary  # noqa: E402


def main(keys) -> None:
    EXPECTED.mkdir(exist_ok=True)
    for key in keys or sorted(CORPUS):
        path = find(CORPUS[key])
        if path is None:
            print(f"{key}: {CORPUS[key]} not found — skipped")
            continue
        new = json.loads(json.dumps(summary(path), default=str))
        out = EXPECTED / f"{key}.json"
        old = json.loads(out.read_text()) if out.exists() else None
        out.write_text(json.dumps(new, indent=1), encoding="utf-8")
        if old is None:
            print(f"{key}: frozen for the first time ({len(new['findings'])} findings, {len(new['laps'])} laps)")
            continue
        changed = [k for k in new if old.get(k) != new[k]]
        print(f"{key}: " + ("unchanged" if not changed else "changed: " + ", ".join(changed)))
        for k in changed:
            if k == "findings":
                before, after = {tuple(x) for x in old[k]}, {tuple(x) for x in new[k]}
                for x in sorted(before - after):
                    print(f"   - {x}")
                for x in sorted(after - before):
                    print(f"   + {x}")


if __name__ == "__main__":
    main(sys.argv[1:])
