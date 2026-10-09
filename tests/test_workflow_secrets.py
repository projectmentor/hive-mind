"""No workflow job that runs the test suite may reference `secrets.` (hive-mind-private #56).

The suite is repo code a pull request can change; a job that runs it with a secret in scope hands that
secret to the PR author. Signing therefore lives in its own job (`needs: test`, `environment: signing`)."""
import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.y*ml"))
RUNS_PYTEST = re.compile(r"pytest")
SECRET_REF = re.compile(r"\bsecrets\s*[.\[]")


def _strip_comments(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _split_jobs(wf):
    """{job name: its text, comments dropped}. Workflows here are plain block YAML: a job is a
    two-space-indented key under `jobs:`; no YAML library, since CI installs only requirements.txt."""
    text = _strip_comments(wf.read_text())
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", body)
    return dict(zip(parts[1::2], parts[2::2]))


def _jobs():
    return [pytest.param(wf.name, name, text, id=f"{wf.name}:{name}")
            for wf in WORKFLOWS for name, text in _split_jobs(wf).items()]


def test_workflows_found():
    assert {w.name for w in WORKFLOWS} >= {"ci.yml", "sign.yml"}


@pytest.mark.parametrize("wf,name,text", _jobs())
def test_pytest_job_references_no_secrets(wf, name, text):
    if RUNS_PYTEST.search(text):
        assert not SECRET_REF.search(text), f"{wf}:{name} runs pytest and references secrets."


def test_sign_job_runs_no_suite_and_uses_the_environment():
    jobs = _split_jobs(next(w for w in WORKFLOWS if w.name == "sign.yml"))
    sign = jobs["sign"]
    assert re.search(r"(?m)^    environment: signing$", sign) and re.search(r"(?m)^    needs: test$", sign)
    assert "pytest" not in sign.replace("HIVE_SIGN_SKIP_TESTS", "")
    assert "HIVE_SIGN_SKIP_TESTS" in sign                 # sign_release.py's own gate would run pytest here
    assert "pytest" in jobs["test"] and not SECRET_REF.search(jobs["test"])
