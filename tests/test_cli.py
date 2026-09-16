import pytest

import serve


class StubServer:
    # main() ends in serve_forever(), which never returns. everything worth
    # testing in main() happens before that, so the socket is the one thing
    # stubbed out.
    last = None

    def __init__(self, address, handler):
        self.address = address
        self.handler = handler
        self.served = False
        StubServer.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def serve_forever(self):
        self.served = True


@pytest.fixture
def run_main(monkeypatch):
    monkeypatch.setattr(serve, "Server", StubServer)
    dodo_before = serve.DODO_URL

    def _run(argv, env=None):
        monkeypatch.setattr(serve.sys, "argv", ["serve.py"] + argv)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        serve.main()
        return StubServer.last

    yield _run
    serve.DODO_URL = dodo_before


def test_a_mount_is_registered_under_its_prefix(run_main, mapdata):
    run_main(["--mount", f"/econ/={mapdata['econ']}"])
    assert serve.MOUNTS["/econ/"] == str(mapdata["econ"])


def test_a_prefix_without_a_trailing_slash_gets_one(run_main, mapdata):
    # "/econ" and "/econ/" must not be two different mounts
    run_main(["--mount", f"/econ={mapdata['econ']}"])
    assert list(serve.MOUNTS) == ["/econ/"]


def test_a_missing_mount_directory_is_not_fatal(run_main, mapdata, capsys):
    # serving three of four datasets beats refusing to start
    run_main(["--mount", f"/gone/={mapdata['tmp'] / 'nope'}",
              "--mount", f"/econ/={mapdata['econ']}"])
    assert list(serve.MOUNTS) == ["/econ/"]
    assert "MISSING -- requests will 404" in capsys.readouterr().out


def test_a_mount_without_an_equals_sign_is_refused(run_main, mapdata):
    with pytest.raises(SystemExit) as exc:
        run_main(["--mount", "/econ/"])
    assert "wants PREFIX=DIR" in str(exc.value)


def test_mounts_can_come_from_the_environment(run_main, mapdata):
    # a container declares its volumes without a bespoke command line
    run_main([], env={"KINGFISHER_MOUNTS":
                      f"/econ/={mapdata['econ']},/layers/={mapdata['layers']}"})
    assert set(serve.MOUNTS) == {"/econ/", "/layers/"}


def test_environment_and_flags_compose(run_main, mapdata):
    run_main(["--mount", f"/econ/={mapdata['econ']}"],
             env={"KINGFISHER_MOUNTS": f"/layers/={mapdata['layers']}"})
    assert set(serve.MOUNTS) == {"/econ/", "/layers/"}


def test_an_empty_mounts_variable_is_ignored(run_main, mapdata):
    run_main(["--mount", f"/econ/={mapdata['econ']}"],
             env={"KINGFISHER_MOUNTS": "  "})
    assert set(serve.MOUNTS) == {"/econ/"}


def test_root_is_resolved_to_a_real_path(run_main, mapdata):
    run_main(["--root", str(mapdata["viewer"])])
    assert serve.ROOT == str(mapdata["viewer"].resolve())


def test_bind_and_port_reach_the_server(run_main, mapdata):
    stub = run_main(["--bind", "0.0.0.0", "--port", "15099"])
    assert stub.address == ("0.0.0.0", 15099)
    assert stub.served is True


def test_port_and_bind_default_from_the_environment(run_main, mapdata):
    stub = run_main([], env={"KINGFISHER_PORT": "15055",
                             "KINGFISHER_BIND": "127.0.0.2"})
    assert stub.address == ("127.0.0.2", 15055)


def test_the_dodo_back_link_is_configurable(run_main, mapdata):
    # the answer differs depending on how you got here: loopback from the
    # laptop, or a traefik hostname from a phone
    run_main(["--dodo-url", "http://dodo.localhost:8800/"])
    assert serve.DODO_URL == "http://dodo.localhost:8800/"


def test_starting_with_no_mounts_says_so(run_main, capsys):
    run_main([])
    assert "mount      none declared" in capsys.readouterr().out


def test_startup_reports_what_each_mount_holds(run_main, mapdata, capsys):
    run_main(["--mount", f"/econ/={mapdata['econ']}"])
    out = capsys.readouterr().out
    assert "13 series" in out
    assert "3 chunks" in out
    assert "7 files" in out


def test_ctrl_c_stops_cleanly(run_main, mapdata, capsys, monkeypatch):
    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(StubServer, "serve_forever", interrupt)
    run_main([])
    assert "stopped" in capsys.readouterr().out
