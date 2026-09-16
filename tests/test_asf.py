# the ASF adapter, tested -- it shipped without these once and the operator asked
# "are you also adding unit tests??", which is the correct question.

import json

import providers
import serve


class Flags:
    def check(self, key):
        return True


JSONLITE = {"results": [
    {"granuleName": "UA_Haywrd_05502_18039-006_20024-029_0771d_s01_L090_01",
     "browse": ["https://datapool.asf.alaska.edu/BROWSE/UA/x.png"],
     "sizeMB": "12.5", "startTime": "2018-08-01T00:00:00Z"},
    {"granuleName": "UA_NoBrowse_00000_18001-000_19001-000_0365d_s01_L090_01",
     "browse": [], "sizeMB": "9", "startTime": "2018-01-01"},
]}


def test_friendly_name_decodes_the_line_noise():
    t, d = providers._asf_friendly(
        "UA_Haywrd_05502_18039-006_20024-029_0771d_s01_L090_01")
    assert t == "Hayward fault ground deformation, 2018 to 2020 (UAVSAR)"
    assert "771-day baseline" in d
    assert "centimeter-scale" in d


def test_unparseable_granule_degrades_to_honest_generic():
    t, d = providers._asf_friendly("SOMETHING_ELSE")
    assert "SOMETHING_ELSE" in t
    assert "interferogram" in d.lower()


def test_products_skip_browseless_and_carry_prose(monkeypatch):
    monkeypatch.setattr(providers, "_get_json", lambda s_, u, timeout=60: JSONLITE)
    out = providers._asf_products((-122.6, 37.05, -121.55, 38.07), 10)
    assert len(out) == 1  # the browse-less product is SKIPPED, not auth-walled
    p = out[0]
    assert p["title"].startswith("Hayward fault ground deformation")
    assert p["description"]
    assert p["download_url"].endswith(".png")
    assert p["bytes_estimate"] == 12500000
    assert p["vintage_id"] == "2018-08-01"
