# The documents describe the kingfisher that exists.
#
# Prose cannot be tested for truth. What can be pinned is that the documents
# name the roots the manifests mount, by the names the env file gives them;
# that the base manifest names no machine; that the scripts the documents
# point at exist and do what they say; and that the control script answers
# the verbs an operator needs.

import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


# ---------------------------------------------------------------------------
# the documents name the machine that exists


def test_operation_md_exists_and_names_every_workload():
    text = _read("OPERATION.md")
    for name in ("kingfisher", "kingfisher-ingestd", "kingfisher-gibsd",
                 "kingfisher-openskyd", "kingfisher-weatherd"):
        assert f"`{name}`" in text, name
    for port in ("15021", "15022", "15023", "15024", "15025"):
        assert port in text, port


@pytest.mark.parametrize("doc", ["USING.md", "OPERATION.md"])
def test_documents_name_the_roots_by_the_names_the_env_file_gives_them(doc):
    """The template mounts under ${DATA_ROOT}, ${SHELF_ROOT} and ${UI_ROOT};
    the documents say the same names, so a reader can map a page to their
    own env file without a translation table."""
    text = _read(doc)
    template = _read("kube", "template", "deployment-patch.yaml")
    for root in sorted(set(re.findall(r"\$\{([A-Z_]+_ROOT)\}", template))):
        assert root in text, f"{doc} does not mention {root}, which the template mounts under"


def test_the_fleet_deploy_is_gone():
    # namespace fleet does not exist; a kustomization deploying into it is a
    # footgun that renders cleanly and lands nowhere
    assert not os.path.exists(os.path.join(ROOT, "kube", "kustomization.yaml"))
    base = _read("kube", "base", "deployment.yaml")
    assert "namespace: fleet" not in base
    for p in re.findall(r"hostPath:\s*\n\s*path: (\S+)", base):
        assert p.startswith("/srv/kingfisher/"), f"the base manifest names a machine: {p}"


def test_the_selector_warning_survived_the_move():
    # the one migration that is an outage was documented on the deleted file;
    # the warning has to live where the selector does
    text = _read("kube", "base", "kustomization.yaml")
    assert "commonLabels" in text and "selector" in text


@pytest.mark.parametrize("doc", ["README.md", "tests/test_endpoints.py"])
def test_delightd_is_only_mentioned_as_history(doc):
    # prose wraps, so judge the sentence around each mention, not the line
    flat = " ".join(_read(*doc.split("/")).split())
    for m in re.finditer("delightd", flat):
        window = flat[max(0, m.start() - 160): m.end() + 160]
        assert "turned down" in window, window


# ---------------------------------------------------------------------------
# the scripts the documents point at


def test_roll_script_gates_on_in_flight_downloads():
    """fable-reviewer's suggestion, 2026-09-04: the roll-time hazard from
    issue 79 belongs in the deploy path, not in a runbook someone remembers."""
    text = _read("bin", "roll.sh")
    assert "FETCH_STATE_QUEUED" in text and "FETCH_STATE_FETCHING" in text
    assert "bin/build.sh" in text, "roll must build through the one path that stamps the commit"
    # the apply names the TARGET's overlay, with the context explicit
    assert 'kubectl kustomize "kube/$TARGET"' in text and "$KUBECTL apply -f -" in text, "a roll that never applies is a restart with a better name"
    assert text.index('kubectl kustomize "kube/$TARGET"') < text.index("rollout restart"), "manifests before restart"
    assert "/health" in text, "roll must prove the commit it rolled"
    assert os.access(os.path.join(ROOT, "bin", "roll.sh"), os.X_OK)


def test_install_script_links_the_control_script_where_the_estate_looks():
    text = _read("bin", "install.sh")
    assert ".claude/init.claude" in text
    assert 'ln -sfn "$REPO/bin/kingfisher"' in text, "link, not copy: one file to edit"
    assert os.access(os.path.join(ROOT, "bin", "install.sh"), os.X_OK)


def test_the_control_script_answers_the_standard_verbs():
    """the house rule: start, stop, status and so forth, working when
    Claude is not around -- so this runs it with the bare interpreter and no
    project environment, exactly as init.claude would."""
    out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "kingfisher"), "--help"],
                         capture_output=True, text=True, timeout=30,
                         env={"PATH": os.environ.get("PATH", "")}).stdout
    for verb in ("start", "stop", "restart", "status", "health", "logs"):
        assert re.search(rf"\b{verb}\b", out), f"{verb} missing from --help"


def test_the_ticket_verb_still_exists_under_its_new_name():
    # `status` used to mean one fetch ticket; the service needed the word
    out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "kingfisher"), "ticket", "--help"],
                         capture_output=True, text=True, timeout=30).stdout
    assert "ticket" in out
