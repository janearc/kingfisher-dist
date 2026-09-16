# every runtime event is one JSON line on stdout -- the collector's contract.

import json

import log


def test_log_emits_one_parseable_json_line(capsys):
    log.log("warn", "valhalla_step_down", rpc="Route", err="refused")
    line = capsys.readouterr().out.strip()
    rec = json.loads(line)
    assert rec["svc"] == "kingfisher"
    assert rec["event"] == "valhalla_step_down"
    assert rec["level"] == "warn"
    assert rec["err"] == "refused"
    assert "ts" in rec
