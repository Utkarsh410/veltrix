"""The workspace: files of any format go in, sessions come out, nothing leaves the machine."""

import http.client
import json
import threading

import pytest

import format_writers as fw
from conftest import GOLDEN
from veltrix_engine.workspace import Workspace, make_server

BEACONS = (80.660, 161.430)
CSV = GOLDEN / "logger_layout_3lap_20hz.csv"


@pytest.fixture(scope="module")
def files(logger):
    ref = logger.channels
    ld = fw.ld([("Ground Speed", "km/h", 20, ref["speed"], ("i2", 2)), ("Engine RPM", "rpm", 20, ref["rpm"], "f4")])
    return {"bus.asc": fw.asc(fw.can_frames(ref)).encode(), "car.dbc": fw.DBC_TEXT.encode(),
            "run.ld": ld, "run.ldx": fw.ldx(BEACONS).encode(), "logger.csv": CSV.read_bytes()}


def test_files_of_every_kind_find_their_place(tmp_path, files):
    ws = Workspace(tmp_path / "ws")

    early = ws.add_file("bus.asc", files["bus.asc"])
    assert early["result"] == "error" and "add the .dbc file first" in early["message"]
    assert ws.add_file("car.dbc", files["car.dbc"])["result"] == "dbc"
    can = ws.add_file("bus.asc", files["bus.asc"])
    assert can["result"] == "session" and can["session"]["format"] == "Vector ASCII CAN log"

    assert ws.add_file("run.ldx", files["run.ldx"])["result"] == "kept"
    assert ws.state()["waiting"] == ["run.ldx"]
    ld = ws.add_file("run.ld", files["run.ld"])
    assert ld["result"] == "session" and ld["session"]["laps"] == 3, "the waiting .ldx gave the laps"
    assert "already in the project" in ws.add_file("run.ld", files["run.ld"])["message"]

    csv = ws.add_file("../../outside/logger.csv", files["logger.csv"])
    assert csv["result"] == "session" and csv["session"]["name"] == "logger.csv"
    assert not (tmp_path / "outside").exists(), "a file name cannot reach outside the workspace"
    assert ws.add_file("notes.dbc", b"not a database")["result"] == "error"
    assert ws.add_file("junk.bin", bytes(range(256)) * 8)["result"] == "error"

    state = ws.state()
    assert [s["format"] for s in state["sessions"]] == ["Vector ASCII CAN log", "MoTeC .ld", "CSV"]
    assert [d["name"] for d in state["dbc"]] == ["car.dbc"] and state["dbc"][0]["signals"] == 9
    assert not list(ws.inbox.iterdir()), "nothing is left behind in the inbox"
    json.dumps(state)

    detail = ws.session(can["session"]["id"])
    assert detail["dbc"] == ["car.dbc"] and len(detail["laps"]) == 3
    assert {"channel": "speed", "column": "Speed", "unit": "km/h"} in detail["channels"]
    assert any("CAN frames" in n["message"] for n in detail["notes"])

    report = ws.report(csv["session"]["id"])
    assert report.stat().st_size > 10_000 and ws.report(csv["session"]["id"]) == report
    ws.delete_session(ld["session"]["id"])
    assert len(ws.sessions()) == 2


def test_server_serves_only_its_own_page(tmp_path, files):
    server = make_server(tmp_path / "ws", port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    try:
        status, page = call("GET", "/")
        assert status == 200 and b"Veltrix workspace" in page
        assert call("POST", "/api/files?name=logger.csv", files["logger.csv"])[0] == 403, "no header, no upload"
        assert call("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
        status, body = call("POST", "/api/files?name=logger.csv", files["logger.csv"], {"X-Veltrix": "1"})
        result = json.loads(body)
        assert status == 200 and result["result"] == "session" and result["session"]["laps"] == 3
        state = json.loads(call("GET", "/api/state")[1])
        assert len(state["sessions"]) == 1 and ".ibt" in state["extensions"]
        sid = result["session"]["id"]
        assert json.loads(call("GET", f"/api/sessions/{sid}")[1])["format"] == "CSV"
        status, report = call("GET", f"/report/{sid}")
        assert status == 200 and len(report) > 10_000
        assert call("GET", "/report/99")[0] == 404
        assert call("POST", f"/api/sessions/{sid}/delete", headers={"X-Veltrix": "1"})[0] == 200
        assert json.loads(call("GET", "/api/state")[1])["sessions"] == []
    finally:
        server.shutdown()
        server.server_close()
