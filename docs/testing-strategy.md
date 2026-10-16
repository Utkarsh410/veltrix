# Testing strategy

Run everything with `.venv/bin/python -m pytest` (about 30 s).

## Layers of tests

| Layer | What it protects | Where |
|---|---|---|
| Golden outputs | Numerical results on three frozen sessions, to a relative 1e-6: lap table, channel samples, sectors, delta, G-G, tyres, models | `tests/test_golden.py`, `datasets/expected/*.json` |
| Behaviour | Each engine rule on a small staged case (a lap that cuts the track, a mislabelled unit, a staged slide) | `tests/test_engine.py`, `test_real_export_cases.py`, `test_corners.py`, `test_handling.py`, `test_advice.py`, `test_engineering.py` |
| Formats | Each importer against the same known session written in that format | `tests/test_formats.py`, `tests/format_writers.py` |
| Model | Canonical model invariants: layers, provenance, native timestamps, persistence | `tests/test_model.py` |
| Store and workspace | Round trips, immutability of sources, the local server | `tests/test_store.py`, `test_workspace.py` |
| Reference session | Headline numbers and timings on the large real session; skipped when the file is not on the machine | `tests/test_reference_session.py` |

## Rules

1. **Regression first.** Before a calculation is touched, its current output is
   pinned by a test. If the output then changes, the commit says which of
   these it is: bug fix, new algorithm, new assumption, new interpretation of
   an input. Golden files are re-frozen (`tools/freeze_expected.py`) only with
   that statement.
2. **Synthetic data is labelled.** A test that builds its own data says what
   properties the data has (`staged_session`, `format_writers`). Synthetic
   files prove the reader agrees with the writer, not that the reader agrees
   with a logger; the README lists which formats have only that.
3. **No real data in the repo beyond the golden sessions.** Tests that need a
   large real file find it through `VELTRIX_REFERENCE_SESSION` (or the known
   path in `~/Downloads`) and skip, visibly, when it is absent.
4. **A feature counts when its path runs in a test.** Not when the code exists.
5. **Performance is measured, not asserted.** `benchmarks/` prints timings with
   machine details; tests only guard against order-of-magnitude regressions.

## End-to-end paths to exist by Phase 6

- CSV → model → maths → lap compare → events → report
- MDF → mapping → model → workspace → report
- session → video → sync → seek
- workspace save → close → reopen

## Not covered today

- Any UI behaviour (the HTML pages are syntax-checked, not driven).
- MoTeC `.ld` and iRacing `.ibt` against real files.
- Concurrency and cancellation (no background tasks exist yet).
