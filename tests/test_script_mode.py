# serve.py runs as a SCRIPT in the container, not as an import. The suite
# imports it, so a definition stranded below the __main__ block loads fine
# under pytest and is unreachable in production -- which happened, and the
# first RPC in the cluster answered NameError. This test runs it the way the
# container does and asks for the two surfaces that live furthest down the
# file: an RPC and the discovery blob.

import json
import subprocess
import sys
import time
import urllib.request


def test_script_mode_serves_the_whole_surface(tmp_path):
    port = 15991
    proc = subprocess.Popen(
        [sys.executable, "serve.py", "--port", str(port),
         "--bind", "127.0.0.1", "--root", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        req = urllib.request.Request(
            base + "/kingfisher.routing.v1.RoutingService/GetCapability",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.load(r)
        assert "MODE_AUTO" in out["capability"]["modes"]
        with urllib.request.urlopen(base + "/discovery", timeout=5) as r:
            assert len(json.load(r)["sources"]) >= 59
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_script_mode_starts_the_heartbeat_thread(tmp_path):
    # the wiring defect this pins: _start_heartbeat DEFINED but never
    # CALLED shipped to the cluster -- a silent replace failure. The script
    # entrypoint must actually start the thread (here it prints the
    # not-configured line, which proves the call happened).
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['serve.py','--port','0','--bind','127.0.0.1']; "
         "import os, threading, serve; "
         # os._exit, not sys.exit: SystemExit in a timer thread cannot stop
         # serve_forever in the main thread, and the subprocess never ends
         "threading.Timer(0.7, lambda: os._exit(0)).start(); "
         "serve.main()"],
        capture_output=True, text=True, timeout=15, cwd=".")
    # the not-configured notice is a structured event on STDOUT now
    assert "heartbeat_disabled" in out.stdout
