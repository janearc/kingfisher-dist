# The index has to survive the pod. Everything here pins a failure that was
# live on 2026-08-30: /discovery answered 64 sources and ZERO datasets after
# five restarts, so every RefreshIndex anyone had run was gone and kingfisher
# looked like it was ignoring flags that were on the whole time.

import json

import providers


def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("KINGFISHER_INDEX", str(tmp_path / "index.json"))
    providers._INDEX.clear()
    providers._TICKETS.clear()


def test_index_survives_a_restart(tmp_path, monkeypatch):
    _clean(tmp_path, monkeypatch)
    with providers._INDEX_LOCK:
        providers._INDEX["usgs-1"] = {
            "id": "usgs-1", "source_id": "usgs", "title": "a lidar tile",
            "state": "FETCH_STATE_INDEXED", "bytes_estimate": 12,
        }
        providers._save_index_locked()

    # the restart: the process forgets everything and reads the file back
    providers._INDEX.clear()
    assert providers.list_datasets() == []
    assert providers.load_index() == 1
    rows = providers.list_datasets()
    assert [r["id"] for r in rows] == ["usgs-1"]
    assert rows[0]["title"] == "a lidar tile"


def test_an_interrupted_fetch_loads_as_failed_not_fetching(tmp_path, monkeypatch):
    # a dataset mid-download when the pod died has no thread behind it any
    # more. Leaving it FETCHING makes every display wait forever on work that
    # is not happening; FAILED is honest and retryable.
    _clean(tmp_path, monkeypatch)
    (tmp_path / "index.json").write_text(json.dumps({"datasets": [
        {"id": "usgs-2", "source_id": "usgs", "title": "interrupted",
         "state": "FETCH_STATE_FETCHING"},
        {"id": "usgs-3", "source_id": "usgs", "title": "fine",
         "state": "FETCH_STATE_HELD"},
    ]}))
    providers.load_index()
    by = {d["id"]: d["state"] for d in providers.list_datasets()}
    assert by["usgs-2"] == "FETCH_STATE_FAILED"
    assert by["usgs-3"] == "FETCH_STATE_HELD"


def test_a_missing_index_is_not_an_error(tmp_path, monkeypatch):
    # the store starts empty by design; an absent file is the normal first boot
    _clean(tmp_path, monkeypatch)
    assert providers.load_index() == 0


def test_an_unreadable_index_does_not_take_the_pod_down(tmp_path, monkeypatch):
    _clean(tmp_path, monkeypatch)
    (tmp_path / "index.json").write_text("{this is not json")
    assert providers.load_index() == 0


def test_refresh_index_persists_what_it_learned(tmp_path, monkeypatch):
    # the actual regression: refresh_index wrote only to memory, so the
    # catalogue it fetched died with the process.
    _clean(tmp_path, monkeypatch)

    class Flags:
        def check(self, key):
            return True

    monkeypatch.setattr(providers, "_usgs_products",
                        lambda bbox, limit: [{"id": "usgs-9", "source_id": "usgs",
                                              "title": "t", "state": "FETCH_STATE_INDEXED"}])
    out = providers.refresh_index("usgs", Flags(), bbox=None, limit=1)
    assert out == {"indexed": 1}

    providers._INDEX.clear()
    providers.load_index()
    assert [d["id"] for d in providers.list_datasets()] == ["usgs-9"]


def test_fetch_is_queued_before_it_is_fetching(tmp_path, monkeypatch):
    # QUEUED and FETCHING are different states because a display that cannot
    # tell them apart draws a spinner for both, and a backlog of six behind one
    # slow download reads as six stalls.
    _clean(tmp_path, monkeypatch)
    with providers._INDEX_LOCK:
        providers._INDEX["usgs-4"] = {
            "id": "usgs-4", "source_id": "usgs", "title": "big",
            "state": "FETCH_STATE_INDEXED", "download_url": "http://example.invalid/x",
        }

    class Flags:
        def check(self, key):
            return True

    # the worker never runs: we are asserting on what start_fetch does BEFORE
    # anything is downloading, which is the state the display sees first.
    monkeypatch.setattr(providers, "_ensure_worker", lambda: None)
    out = providers.start_fetch("usgs-4", Flags(), str(tmp_path / "spool"))
    assert "ticket" in out
    assert out["queued_behind"] == 0
    assert providers._INDEX["usgs-4"]["state"] == "FETCH_STATE_QUEUED"
    assert providers._INDEX["usgs-4"]["bytes_downloaded"] == 0
    assert providers.queue_depth() == 1
    # drain, so the queue does not leak into another test
    providers._FETCH_Q.get(); providers._FETCH_Q.task_done()


def test_one_worker_means_downloads_are_serial(tmp_path, monkeypatch):
    # DESIGN.md already ruled serial for this class of work; start_fetch used
    # to spawn a thread per call, so N clicks were N concurrent transfers.
    _clean(tmp_path, monkeypatch)
    providers._ensure_worker()
    first = providers._WORKER
    providers._ensure_worker()
    assert providers._WORKER is first, "a second call must not start a second worker"
    assert first.is_alive()


def test_re_indexing_does_not_forget_what_is_held(tmp_path, monkeypatch):
    # the provider's catalogue is authoritative about titles and urls and knows
    # nothing about whether we hold the bytes. Overwriting the record wholesale
    # reset a HELD dataset to INDEXED, so the display offered to re-download a
    # 363MB tile that was already on disk and already hexed.
    _clean(tmp_path, monkeypatch)
    with providers._INDEX_LOCK:
        providers._INDEX["usgs-5"] = {
            "id": "usgs-5", "source_id": "usgs", "title": "old title",
            "state": "FETCH_STATE_HELD", "bytes_downloaded": 12345,
        }

    class Flags:
        def check(self, key):
            return True

    monkeypatch.setattr(providers, "_usgs_products",
                        lambda bbox, limit: [{"id": "usgs-5", "source_id": "usgs",
                                              "title": "fresh title from the provider",
                                              "state": "FETCH_STATE_INDEXED"}])
    providers.refresh_index("usgs", Flags(), bbox=None, limit=1)
    row = providers.list_datasets()[0]
    assert row["state"] == "FETCH_STATE_HELD", "our fetch state survives"
    assert row["bytes_downloaded"] == 12345
    assert row["title"] == "fresh title from the provider", "their metadata wins"


def test_a_yearly_layer_is_asked_for_a_year_it_has(tmp_path, monkeypatch):
    # asking every GIBS layer for "yesterday" is right for daily imagery and
    # wrong for everything else, and NASA answers an out-of-range date with
    # 200 and an empty frame -- so it fails as 28KB of black with no error.
    # Black Marble is yearly, 2012 and 2016.
    meta = {"period": "yearly",
            "startDate": "2012-01-01T00:00:00Z", "endDate": "2017-01-01T23:59:59Z",
            "dateRanges": [
                {"startDate": "2012-01-01T00:00:00Z", "endDate": "2013-01-01T23:59:59Z"},
                {"startDate": "2016-01-01T00:00:00Z", "endDate": "2017-01-01T23:59:59Z"}]}
    assert providers._layer_date(meta, "2026-08-30") == "2016-01-01", "the newest range it holds"
    # a layer with no ranges keeps the caller's fallback
    assert providers._layer_date({}, "2026-08-30") == "2026-08-30"
    # ranges beat the outer endDate, which for this layer is an exclusive bound
    assert providers._layer_date(meta, "x") != "2017-01-01"
