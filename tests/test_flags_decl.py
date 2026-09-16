# The service is the source of what it declares. flags_decl.py holds the one
# list of flags kingfisher publishes to flipr, every process publishes it at
# startup, and bin/publish-flags.sh publishes the same list by hand. These
# tests pin the list against the code that consults it, the description
# template flipr enforces, the never-raise contract of the startup publish,
# and that every entry point actually calls it.

import os
import re

import flags_decl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMONS = ("gibsd.py", "openskyd.py", "weatherd.py", "ingestd.py")


def _read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def test_every_flag_the_code_consults_by_name_is_declared():
    consulted = set()
    for name in os.listdir(ROOT):
        if name.endswith(".py") and name != "flags_decl.py":
            consulted |= set(re.findall(r'check\("([a-z_.]+)"\)', _read(name)))
    assert consulted, "the grep found nothing; the pattern or the tree moved"
    assert consulted <= flags_decl.KEYS, consulted - flags_decl.KEYS


def test_the_list_is_the_namespace_the_cluster_carries():
    # the fourteen keys kingfisher@v1 held; a change
    # here is a change to what an operator can switch, and is deliberate
    assert flags_decl.KEYS == {
        "network.enabled", "fetch.enabled", "fetch.usgs", "fetch.noaa", "fetch.overture",
        "fetch.nasa", "fetch.carto", "fetch.nasa_gibs", "fetch.asf", "fetch.opensky",
        "fetch.weather", "ingest.enabled", "routing.enabled", "routing.valhalla"}
    assert flags_decl.SERVICE == "kingfisher" and flags_decl.VERSION == "v1"


def test_descriptions_follow_the_on_off_template_flipr_enforces():
    for f in flags_decl.FLAGS:
        assert f["description"].startswith("on: "), f["key"]
        assert " off: " in f["description"], f["key"]
        assert "kill" not in f["description"].split(" off: ")[0].lower(), f["key"]
        assert isinstance(f["value"]["boolValue"], bool) and isinstance(f["expensive"], bool), f["key"]


class _Client:
    def __init__(self, result):
        self.result, self.calls = result, []

    def publish(self, flags):
        self.calls.append(flags)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_publish_reports_the_count_and_logs_it():
    lines = []
    c = _Client(14)
    assert flags_decl.publish(c, log_fn=lambda lvl, ev, **kw: lines.append((lvl, ev, kw))) == 14
    assert c.calls == [flags_decl.FLAGS]
    assert lines == [("info", "flags_published", {"namespace": "kingfisher@v1", "count": 14})]


def test_publish_never_raises_when_flipr_is_down():
    lines = []
    c = _Client(ConnectionError("flipr.flipr: connection refused"))
    assert flags_decl.publish(c, log_fn=lambda lvl, ev, **kw: lines.append((lvl, ev, kw))) == -1
    assert lines[0][:2] == ("warn", "flags_publish_failed")
    assert "connection refused" in lines[0][2]["err"]


def test_every_daemon_publishes_at_startup_before_its_loop():
    for d in DAEMONS:
        text = _read(d)
        main = text.split("def main():")[1]
        assert "flags_decl.publish(_flags())" in main, d
        # the heartbeat is up first (the lease), then the declaration, then the loop
        assert main.index("heartbeat.start(") < main.index("flags_decl.publish(") < main.index("while True:"), d


def test_the_server_publishes_off_the_request_path():
    text = _read("serve.py")
    assert "threading.Thread(target=flags_decl.publish, args=(flipr_flags(),), daemon=True).start()" in text


def test_the_hand_script_uses_the_module_and_carries_no_list():
    text = _read("bin/publish-flags.sh")
    assert "flags_decl.publish(flags_decl.client())" in text
    assert "bflag(" not in text and "FLAGS = [" not in text
