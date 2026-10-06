"""Shared fixtures: the golden datasets, loaded once per test run."""

import json
from pathlib import Path

import pytest

from veltrix_engine import load_session

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "datasets" / "golden"
EXPECTED = ROOT / "datasets" / "expected"

DATASETS = {
    "synthetic": "synthetic_gt3_5lap.csv.gz",
    "logger": "logger_layout_3lap_20hz.csv",
    "monza": "monza_ac_gt3.csv.gz",
}


@pytest.fixture(scope="session")
def sessions():
    return {key: load_session(GOLDEN / name) for key, name in DATASETS.items()}


@pytest.fixture(scope="session")
def synthetic(sessions):
    return sessions["synthetic"]


@pytest.fixture(scope="session")
def logger(sessions):
    return sessions["logger"]


@pytest.fixture(scope="session")
def monza(sessions):
    return sessions["monza"]


def expected(key: str) -> dict:
    return json.loads((EXPECTED / (DATASETS[key].split(".csv")[0] + ".json")).read_text())
