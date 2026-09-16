# _dodo_url_for: the escape link derived from the request, never from an
# environment conditional. Mirrors lib/themes/nav.mjs's dodoOrigin()
# (janearc/dodo) -- see serve.py's own comment for why this exists in
# Python rather than importing that module.

import serve


def test_test_tld_derives_same_network_dodo():
    assert serve._dodo_url_for("kingfisher.test:9800") == "http://dodo.test:9800/"


def test_localhost_tld_derives_same_network_dodo():
    assert serve._dodo_url_for("kingfisher.localhost:8800") == "http://dodo.localhost:8800/"


def test_no_port_omits_the_colon():
    assert serve._dodo_url_for("kingfisher.test") == "http://dodo.test/"


def test_bare_ip_falls_back_to_configured_default():
    assert serve._dodo_url_for("127.0.0.1:9800") == serve.DODO_URL


def test_unrecognised_tld_falls_back_rather_than_link_wrong():
    assert serve._dodo_url_for("kingfisher.example.com") == serve.DODO_URL


def test_missing_host_header_falls_back():
    assert serve._dodo_url_for(None) == serve.DODO_URL
    assert serve._dodo_url_for("") == serve.DODO_URL
