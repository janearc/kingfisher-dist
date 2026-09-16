# Issue 70: the accept queue was five deep and every tile was a new connection.
#
# Server never set request_queue_size, so the listen backlog was socketserver's
# default of 5, and the handler spoke HTTP/1.0 so nothing was reused. A browser
# map pane opens six connections per host. Inside the serving pod after two
# days: ListenOverflows=102, ListenDrops=102, and /health said 200 throughout.

import http.client
import threading
import urllib.parse

import serve


def test_the_backlog_is_deep_enough_for_a_browser():
    assert serve.Server.request_queue_size == 128


def test_the_handler_speaks_http_1_1():
    assert serve.Handler.protocol_version == "HTTP/1.1"


def _conn(server):
    u = urllib.parse.urlparse(server)
    return http.client.HTTPConnection(u.hostname, u.port, timeout=10)


def test_two_requests_share_one_connection(server):
    """Keep-alive, end to end. This is also the test that every response
    carries content-length: a response without one would leave the client
    waiting for a body that never ends, and the second request would hang."""
    c = _conn(server)
    c.request("GET", "/health")
    r1 = c.getresponse(); b1 = r1.read()
    c.request("GET", "/econ/")
    r2 = c.getresponse(); b2 = r2.read()
    c.close()
    assert r1.status == 200 and b1
    assert r2.status == 200 and b2
    assert r1.getheader("connection", "").lower() != "close"


def test_a_404_does_not_break_the_connection(server):
    # error responses go through send_error; they must carry a length too
    c = _conn(server)
    c.request("GET", "/definitely-not-here.json")
    r1 = c.getresponse(); r1.read()
    c.request("GET", "/health")
    r2 = c.getresponse(); r2.read()
    c.close()
    assert r1.status == 404 and r2.status == 200


def test_a_range_response_does_not_break_the_connection(server, mounted):
    # 206 is the response most likely to be hand-built; it must be framed too
    c = _conn(server)
    c.request("GET", "/econ/", headers={"Range": "bytes=0-9"})
    r1 = c.getresponse(); r1.read()
    c.request("GET", "/health")
    r2 = c.getresponse(); r2.read()
    c.close()
    assert r1.status in (200, 206)
    assert r2.status == 200


def test_many_simultaneous_connections_all_get_served(server):
    """The shape of the live failure: N clients at the same instant. With a
    backlog of 5 some were reset; with 128 none are."""
    results = []
    def hit():
        try:
            c = _conn(server); c.request("GET", "/health")
            results.append(c.getresponse().status); c.close()
        except Exception as e:  # noqa: BLE001 -- the point is to count them
            results.append(repr(e))
    threads = [threading.Thread(target=hit) for _ in range(24)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)
    assert results.count(200) == 24, results
