import pytest

import serve

# The shared look comes from bench-ui (janearc/bench-ui) as a file on disk. What
# matters here is not that the page is pretty -- it is that the sheet is read at
# REQUEST time so installing is the whole deployment, that kingfisher's own two
# rule weights survive, and that a missing sheet degrades loudly instead of
# either dying or going quiet.


def test_the_sheet_is_inlined_into_the_landing_page(get, bench_sheet):
    text = get("/").text
    assert "test double for bench-ui" in text
    # inlined, not linked: one request, and it cannot half-load
    assert "<link" not in text


def test_kingfisher_keeps_its_two_rule_weights(get):
    # this page has no raised surface anywhere. the 2px header underline and the
    # 1px row divider ARE its identity, which is why bench-ui carries separate
    # strong and faint rule tokens at all.
    text = get("/").text
    assert "2px solid var(--rule-strong)" in text
    assert "1px solid var(--rule-faint)" in text


def test_the_page_spends_tokens_and_declares_no_colours_of_its_own(get):
    # the whole point of adopting: one fewer copy of #0b0e14 in the estate.
    # kingfisher's own block must reference tokens, never literals.
    own = serve.KINGFISHER_CSS
    literals = [line for line in own.splitlines()
                if "#" in line and "nostyle" not in line and not
                line.strip().startswith(("/*", "*", "cannot", "to nothing",
                                         "ever renders", "you"))]
    assert literals == []


def test_a_new_sheet_is_picked_up_without_a_restart(get, bench_sheet):
    # installing IS the deployment. the server is already running; rewrite the
    # file underneath it and the very next request must show the new bytes.
    assert "test double for bench-ui" in get("/").text
    bench_sheet.write_text("/* the second install */\n")
    assert "the second install" in get("/").text


def test_the_sheet_is_read_per_request_not_cached_at_startup(get, bench_sheet):
    get("/")
    bench_sheet.write_text("/* third */\n")
    text = get("/").text
    assert "third" in text
    assert "test double" not in text


def test_a_missing_sheet_still_serves_the_page(get, monkeypatch, mapdata):
    # degrade, do not die. this page's job is to say what is wrong with the
    # service, so it is the last thing that should go dark when something is.
    monkeypatch.setenv("KINGFISHER_BENCH_CSS", str(mapdata["tmp"] / "gone.css"))
    resp = get("/")
    assert resp.status == 200
    # the actual content is all still there
    assert "/econ/" in resp.text
    assert "what it knows about" in resp.text


def test_a_missing_sheet_says_so_visibly_and_names_bench_ui(get, monkeypatch,
                                                            mapdata):
    # unstyled-and-says-why is a diagnosis; unstyled-and-quiet is a mystery that
    # looks exactly like a CSS bug in a page nobody has touched.
    monkeypatch.setenv("KINGFISHER_BENCH_CSS", str(mapdata["tmp"] / "gone.css"))
    text = get("/").text
    assert "UNSTYLED" in text
    assert "bench-ui" in text
    # named in the served CSS as well as on the page, the way the other benches
    # do it
    assert "bench-ui stylesheet NOT READ" in text
    # and it says WHICH path failed, so the next step is obvious
    assert "gone.css" in text


def test_the_degraded_warning_does_not_depend_on_the_missing_sheet(get,
                                                                   monkeypatch,
                                                                   mapdata):
    # a warning styled with var(--red) would have no border precisely when the
    # sheet is absent. the one literal colour in the file is deliberate.
    monkeypatch.setenv("KINGFISHER_BENCH_CSS", str(mapdata["tmp"] / "gone.css"))
    assert "#f4664c" in get("/").text


def test_the_override_wins_over_the_default_paths(bench_sheet, monkeypatch):
    css, path, err = serve.bench_css()
    assert err is None
    assert path == str(bench_sheet)
    assert "test double" in css


def test_the_default_paths_are_the_fleet_convention(monkeypatch):
    # /var/mesh-ui is the container mount and matches the host path on purpose;
    # the second is the same file on a laptop, where it lives under $HOME.
    monkeypatch.delenv("KINGFISHER_BENCH_CSS", raising=False)
    assert serve.BENCH_CSS_DEFAULTS[0] == "/var/mesh-ui/bench.css"
    assert serve.BENCH_CSS_DEFAULTS[1] == "~/.config/kingfisher/bench.css"


def test_an_unreadable_sheet_reports_the_path_and_the_reason(monkeypatch,
                                                             mapdata):
    missing = mapdata["tmp"] / "nowhere" / "bench.css"
    monkeypatch.setenv("KINGFISHER_BENCH_CSS", str(missing))
    css, path, err = serve.bench_css()
    assert css is None and path is None
    assert str(missing) in err
    assert "FileNotFoundError" in err


def test_every_default_path_is_tried_before_giving_up(monkeypatch, mapdata):
    # with no override and neither candidate present, the error names EVERY
    # path it tried rather than only the one it happened to give up on
    monkeypatch.delenv("KINGFISHER_BENCH_CSS", raising=False)
    monkeypatch.setattr(serve, "BENCH_CSS_DEFAULTS",
                        (str(mapdata["tmp"] / "a.css"),
                         str(mapdata["tmp"] / "b.css")))
    css, path, err = serve.bench_css()
    assert css is None
    # both are named. in a container the FIRST candidate is the missing mount,
    # which is the one an operator can act on.
    assert "a.css" in err
    assert "b.css" in err


def test_a_later_candidate_is_used_when_an_earlier_one_is_missing(monkeypatch,
                                                                  mapdata):
    good = mapdata["tmp"] / "good.css"
    good.write_text("/* found on the second try */")
    monkeypatch.delenv("KINGFISHER_BENCH_CSS", raising=False)
    monkeypatch.setattr(serve, "BENCH_CSS_DEFAULTS",
                        (str(mapdata["tmp"] / "absent.css"), str(good)))
    css, path, err = serve.bench_css()
    assert err is None
    assert path == str(good)
    assert "second try" in css


def test_the_content_is_wrapped_for_the_shared_layout(get):
    # bench.css puts max-width and column spacing on .wrap, so the page has to
    # provide one or it inherits none of the layout
    text = get("/").text
    assert '<div class="wrap">' in text
    assert text.rstrip().endswith("</div>")
