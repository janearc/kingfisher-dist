# The overlay is generated, never edited.
#
# kube/<target>/ is the render of kube/template/ for one target's env file,
# and a rendered overlay is not committed. These tests pin what makes that
# safe: the example target renders, a render matches itself, drift is named,
# a second target changes only the things that differ between clusters, and
# nothing in a render names a machine.

import importlib.util
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("render_overlay", os.path.join(ROOT, "bin", "render-overlay.py"))
ro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ro)

TEMPLATE_FILES = sorted(os.listdir(os.path.join(ROOT, "kube", "template")))


def _write(t, out):
    os.makedirs(out, exist_ok=True)
    for rel, text in ro.render(t).items():
        with open(os.path.join(out, rel), "w") as f:
            f.write(text)


def test_the_example_environment_ships_and_renders_every_template(tmp_path):
    env = ro.load_env("example")
    assert env["ENV"] == "example" and env["CTX"] == "k3d-example"
    assert env["NAMESPACE"] == "kingfisher"
    assert env["DATA_ROOT"].startswith("/srv/kingfisher")
    out = str(tmp_path / "example")
    _write(env, out)
    assert sorted(os.listdir(out)) == TEMPLATE_FILES
    assert ro.check(env, out) == []


def test_a_render_names_only_its_own_target(tmp_path):
    """A render is meant to be shown to a stranger. Every host path in it is
    under the target's own root, and every namespace in it is the target's."""
    for t in (ro.load_env("example"), ro.target("scratch")):
        for rel, text in ro.render(t).items():
            for p in re.findall(r"hostPath:\s*\n\s*path: (\S+)", text):
                assert p.startswith(t["DATA_ROOT"]) or p.startswith(t["SHELF_ROOT"]) or p.startswith(t["UI_ROOT"]), (rel, p)
            for ns in re.findall(r"namespace: (\S+)", text):
                assert ns == t["NAMESPACE"], (rel, ns)
            for host in re.findall(r"[a-z-]+\.([a-z-]+):\d+", text):
                assert host in (t["NAMESPACE"], "test"), (rel, host)


def test_an_environment_file_missing_a_key_is_refused_by_name(tmp_path):
    p = tmp_path / "half.env"
    p.write_text("ENV=half\nCTX=k3d-half\n")
    with pytest.raises(SystemExit) as ex:
        ro.load_env("half", path=str(p))
    assert "DATA_ROOT" in str(ex.value) and "EDGE_PORT" in str(ex.value)


def test_the_render_names_the_commit_as_a_token_filled_at_apply():
    for t in (ro.load_env("example"), ro.target("scratch")):
        k = ro.render(t)["kustomization.yaml"]
        assert 'newTag: "${COMMIT}"' in k and "name: kingfisher" in k


def test_env_text_round_trips_through_load_env(tmp_path):
    t = ro.target("scratch", edge_port=9900)
    p = tmp_path / "scratch.env"
    p.write_text(ro.env_text(t))
    assert ro.load_env("scratch", path=str(p)) == t


def test_a_second_target_changes_only_what_differs_between_clusters():
    example = ro.render(ro.load_env("example"))
    scratch = ro.render(ro.target("scratch", edge_port=9900))
    patch = scratch["deployment-patch.yaml"]
    # the namespace and every in-cluster name follow the namespace, which is
    # one per machine, so they are the same in both renders
    assert "namespace: kingfisher" in scratch["kustomization.yaml"]
    assert "kafka.kingfisher:9092" in patch and "postgrest.kingfisher:3000" in patch
    # the paths follow the target
    assert "/srv/scratch/maps/ingested" in patch and "/srv/scratch/spool" in patch
    for d in ("ingestd.yaml", "gibsd.yaml", "openskyd.yaml", "weatherd.yaml"):
        assert "/srv/scratch/" in scratch[d], d
        assert "/srv/kingfisher/" not in scratch[d], d
    # the .test names and the container ports do not: a second cluster
    # answers kingfisher.test on a different edge port, matched by host header
    assert "Host(`kingfisher.test`)" in scratch["ingressroute-patch.yaml"]
    assert "port: 15021" in scratch["ingressroute-patch.yaml"]
    assert re.findall(r"Host\(`[^`]+`\)", scratch["ingestd.yaml"]) == re.findall(r"Host\(`[^`]+`\)", example["ingestd.yaml"])
    # the dashboard is <service>-<env> and reads its own prometheus
    assert '"uid": "kingfisher-scratch"' in scratch["dashboard-kingfisher.json"]
    assert '"uid": "scratch-prom"' in scratch["dashboard-kingfisher.json"]
    assert "example-prom" not in scratch["dashboard-kingfisher.json"]


def test_the_serving_pod_mounts_the_shelf_read_only_in_every_target():
    # it only ever reads; the daemons write, under their own root
    for t in (ro.load_env("example"), ro.target("scratch")):
        patch = ro.render(t)["deployment-patch.yaml"]
        mounts = patch.split("volumeMounts:")[1].split("resources:")[0]
        for m in ("/data/eta", "/data/ingested", "/data/flights", "/data/weather", "/data/clouds"):
            after = mounts.split(f"mountPath: {m}")[1].split("- name:")[0]
            assert "readOnly: true" in after, (t["ENV"], m)


def test_a_new_target_takes_the_conventions():
    t = ro.target("scratch")
    assert t["DATA_ROOT"] == "/srv/scratch"
    assert t["SHELF_ROOT"] == "/srv/scratch/maps" and t["UI_ROOT"] == "/srv/scratch/ui"
    assert t["EDGE_PORT"] == "9800" and t["CTX"] == "k3d-scratch"
    assert t["SHELF_READONLY"] == "false"


def test_check_names_the_drift(tmp_path):
    t = ro.target("scratch")
    out = str(tmp_path / "scratch")
    _write(t, out)
    assert ro.check(t, out) == []
    open(os.path.join(out, "ingestd.yaml"), "a").write("# a hand edit\n")
    open(os.path.join(out, "stray.yaml"), "w").write("kind: Nothing\n")
    drift = ro.check(t, out)
    assert "ingestd.yaml" in drift
    assert any(d.startswith("stray.yaml") for d in drift)


def test_the_cli_check_exits_nonzero_on_drift_and_zero_on_a_true_render(tmp_path):
    out = str(tmp_path / "example")
    _write(ro.load_env("example"), out)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "render-overlay.py"), "example", "--check", "--out", out],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # a render to --out for a target with no env file must not write one into
    # the repository as a side effect
    before = set(os.listdir(os.path.join(ROOT, "kube", "environments")))
    out = str(tmp_path / "x")
    _write(ro.target("x"), out)
    open(os.path.join(out, "kustomization.yaml"), "a").write("# edited\n")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "render-overlay.py"), "x", "--check", "--out", out],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "kustomization.yaml" in r.stderr
    assert set(os.listdir(os.path.join(ROOT, "kube", "environments"))) == before


# ---------------------------------------------------------------------------
# the scripts read the target instead of naming a cluster


def _read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def test_roll_reads_the_target_file_and_refuses_a_drifted_overlay():
    text = _read("bin/roll.sh")
    # the target is the first argument and there is no default
    assert 'TARGET="${1:-}"' in text and "usage: bin/roll.sh <env>" in text
    assert '. "kube/environments/$TARGET.env"' in text
    assert 'render-overlay.py "$TARGET" --check' in text
    assert "kubectl --context $CTX" in text
    # the render is applied through kustomize with ${COMMIT} filled by the sha just built
    assert 'kubectl kustomize "kube/$TARGET" | sed "s/\\${COMMIT}/$sha/g" | $KUBECTL apply -f -' in text
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"k3d-[a-z]", code), "roll.sh names a cluster instead of reading $CTX"


def test_roll_proves_health_on_the_targets_edge_port():
    text = _read("bin/roll.sh")
    # EDGE_PORT is the host port that reaches this cluster's edge, always named
    assert 'PORT_SUFFIX=":$EDGE_PORT"' in text and '"80"' not in text
    assert 'f"http://{host}{suffix}/health"' in text


def test_build_tags_the_image_by_sha_and_imports_into_the_target_cluster():
    text = _read("bin/build.sh")
    assert 'docker tag kingfisher:dev "kingfisher:$sha"' in text
    assert 'k3d image import kingfisher:dev "kingfisher:$sha" -c "$TARGET"' in text
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"k3d-[a-z]|-c [a-z]", code), "no default target anywhere"
    # the provenance strings test_version.py pins are untouched
    assert "git rev-parse --short HEAD" in text and "git status --porcelain" in text


def test_the_cli_names_its_context_explicitly():
    text = _read("bin/kingfisher")
    assert 'CONTEXT = os.environ.get("KINGFISHER_CONTEXT", f"k3d-{NAMESPACE}")' in text
    assert '"--context", CONTEXT' in text
