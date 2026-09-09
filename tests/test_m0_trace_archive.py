"""The one-shot lab exporter must preserve a checked-in historical manifest."""

import io
import runpy
import shutil
from pathlib import Path

import pytest


@pytest.mark.parametrize("archive_exists", [True, False])
def test_trace_export_keeps_existing_manifest(tmp_path, monkeypatch, archive_exists):
    root = tmp_path / "checkout"
    scripts = root / "scripts/m0_environment"
    scripts.mkdir(parents=True)
    entry = scripts / "export_traces.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/m0_environment/export_traces.py",
        entry,
    )
    lab = root / "tmp/m0-environment"
    lab.mkdir(parents=True)
    evidence = root / "docs/evidence/m0-real-environment"
    evidence.mkdir(parents=True)
    manifest = evidence / "jaeger-final-export.json"
    original = b'{"historical_run":"preserve-original"}\n'
    if archive_exists:
        manifest.write_bytes(original)
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        return io.BytesIO(b'{"data":[]}')

    monkeypatch.setattr("urllib.request.urlopen", get)
    if archive_exists:
        with pytest.raises(SystemExit):
            runpy.run_path(str(entry), run_name="__main__")
        assert requests == []
        assert manifest.read_bytes() == original
        assert not (lab / "jaeger-final-export").exists()
    else:
        runpy.run_path(str(entry), run_name="__main__")
        assert len(requests) == 1
        assert manifest.is_file()
        assert (
            lab / "jaeger-final-export/services.json"
        ).read_bytes() == b'{"data":[]}'


@pytest.mark.parametrize("archive_exists", [True, False])
def test_observation_keeps_existing_summary(tmp_path, monkeypatch, archive_exists):
    root = tmp_path / "checkout"
    scripts = root / "scripts/m0_environment"
    scripts.mkdir(parents=True)
    entry = scripts / "observe_window.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[1]
        / "scripts/m0_environment/observe_window.py",
        entry,
    )
    evidence = root / "docs/evidence/m0-real-environment"
    evidence.mkdir(parents=True)
    summary = evidence / "recovery-01-observation.json"
    original = b'{"historical_window":"preserve-original"}\n'
    if archive_exists:
        summary.write_bytes(original)
    requests = []

    class Response(io.BytesIO):
        status = 200

    def get(url, **kwargs):
        requests.append(url)
        if "/logs?" in url:
            return Response(b'{"data":{"hits":{"total":0,"hits":[]}}}')
        return Response(b'{"data":[]}')

    monkeypatch.setattr("urllib.request.urlopen", get)
    monkeypatch.setattr("sys.argv", [str(entry), "recovery-01", "1", "301"])
    raw_dir = root / "tmp/m0-environment/raw-evidence/recovery-01"
    if archive_exists:
        with pytest.raises(SystemExit):
            runpy.run_path(str(entry), run_name="__main__")
        assert requests == []
        assert summary.read_bytes() == original
        assert not raw_dir.exists()
    else:
        runpy.run_path(str(entry), run_name="__main__")
        assert len(requests) == 7
        assert summary.is_file()
        assert len(list(raw_dir.iterdir())) == 7
