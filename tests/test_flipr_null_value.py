# Issue 72: one null-valued flag broke every flag read and was blamed on flipr.
#
# flipr accepts SetFlag with no value and serves `"value": null` (issue 54).
# `f.get("value", {})` returns None for a PRESENT null, and `"boolValue" in
# None` raises TypeError -- which is neither FliprDown nor FlagMissing, so every
# daemon's `except Exception` set flipr_ok False and said "flipr unreachable"
# about a flipr that was answering fine.

import pytest

import flipr_client


def _client(flags):
    """A FliprClient whose one round trip returns this namespace."""
    c = flipr_client.FliprClient("http://flipr.test", "kingfisher", "v1")
    c._post = lambda method, body: {"namespace": {"flags": flags}}
    return c


def test_a_present_null_value_is_a_flag_with_no_kind_not_a_crash():
    c = _client([{"key": "network.enabled", "value": None}])
    flags = c.refresh()
    assert flags == {"network.enabled": None}


def test_a_missing_value_key_reads_the_same_as_null():
    c = _client([{"key": "network.enabled"}])
    assert c.refresh() == {"network.enabled": None}


def test_typed_values_still_decode():
    c = _client([
        {"key": "a", "value": {"boolValue": True}},
        {"key": "b", "value": {"stringValue": "x"}},
        {"key": "c", "value": {"intValue": "7"}},
    ])
    assert c.refresh() == {"a": True, "b": "x", "c": 7}


def test_one_null_does_not_take_the_others_down():
    """The failure was total: one half-set flag and the whole namespace read
    raised, so every gate in every daemon failed closed at once."""
    c = _client([
        {"key": "fetch.enabled", "value": {"boolValue": True}},
        {"key": "broken", "value": None},
        {"key": "fetch.usgs", "value": {"boolValue": False}},
    ])
    flags = c.refresh()
    assert flags["fetch.enabled"] is True
    assert flags["fetch.usgs"] is False
    assert flags["broken"] is None


def test_a_null_flag_is_declared_so_check_does_not_raise_missing(monkeypatch):
    # declared-with-no-value is a real state and distinct from undeclared:
    # check() must hand back None, not FlagMissing
    c = _client([{"key": "network.enabled", "value": None}])
    monkeypatch.setattr(c, "ping", lambda: "ok")
    assert c.check("network.enabled") is None
    with pytest.raises(flipr_client.FlagMissing):
        c.check("never.declared")
