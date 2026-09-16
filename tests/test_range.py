# Range serving: a dart is one row of a 954MB table, and one row is 115KB.
# Without ranges the click ships the table.

def test_range_serves_the_window(get, mapdata):
    full = get("/econ/res8.json")
    part = get("/econ/res8.json", headers={"Range": "bytes=0-99"})
    assert part.status == 206
    assert len(part.body) == 100
    assert part.body == full.body[:100]
    assert part.headers["content-range"].startswith("bytes 0-99/")
    assert part.headers["accept-ranges"] == "bytes"


def test_range_open_ended_and_suffix(get):
    full = get("/econ/res8.json").body
    tail = get("/econ/res8.json", headers={"Range": f"bytes={len(full)-50}-"})
    assert tail.status == 206 and tail.body == full[-50:]
    suf = get("/econ/res8.json", headers={"Range": "bytes=-50"})
    assert suf.status == 206 and suf.body == full[-50:]


def test_unsatisfiable_range_416_names_the_size(get):
    full = get("/econ/res8.json").body
    r = get("/econ/res8.json", headers={"Range": f"bytes={len(full)+10}-"})
    assert r.status == 416
    assert r.headers["content-range"] == f"bytes */{len(full)}"


def test_full_requests_unchanged_and_advertise(get):
    r = get("/econ/res8.json")
    assert r.status == 200
    assert r.headers["accept-ranges"] == "bytes"
