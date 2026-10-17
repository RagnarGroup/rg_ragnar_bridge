import hashlib

import pytest

from ragnar_bridge.bridge import Bridge
from ragnar_bridge.config import Config


def _bridge(root):
    return Bridge(Config(url="ws://localhost", token="test", project_paths={"RAG": str(root)}))


def _manifest(path, content):
    return {"path": path, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def test_preview_does_not_write_and_apply_reports_created(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    bridge = _bridge(root)
    mensaje = {"cli": "claude", "project_key": "RAG", "mode": "preview", "files": [_manifest(".claude/agents/reviewer.md", "review") ]}

    preview = bridge._aplicar_bootstrap(mensaje)
    assert preview["files"] == [{"path": ".claude/agents/reviewer.md", "status": "create"}]
    assert not (root / ".claude/agents/reviewer.md").exists()

    mensaje["mode"] = "apply"
    result = bridge._aplicar_bootstrap(mensaje)
    assert result["files"] == [{"path": ".claude/agents/reviewer.md", "status": "written"}]
    assert (root / ".claude/agents/reviewer.md").read_text() == "review"


def test_overwrite_requires_confirmation_and_saves_backup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / ".agent/rules/guide.md"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    bridge = _bridge(root)
    mensaje = {"cli": "agy", "project_key": "RAG", "mode": "apply", "files": [_manifest(".agent/rules/guide.md", "new")]}

    denied = bridge._aplicar_bootstrap(mensaje)
    assert denied["ok"] is False
    assert target.read_text() == "old"

    mensaje["overwrite"] = True
    result = bridge._aplicar_bootstrap(mensaje)
    assert result["files"] == [{"path": ".agent/rules/guide.md", "status": "updated"}]
    backups = list((root / ".ragnar-bootstrap-backups").rglob("guide.md"))
    assert len(backups) == 1 and backups[0].read_text() == "old"


@pytest.mark.parametrize("path", ["../escape", ".claude/agents/../../escape", ".claude/settings.json"])
def test_rejects_paths_outside_allowlist(tmp_path, path):
    root = tmp_path / "repo"
    root.mkdir()
    bridge = _bridge(root)
    with pytest.raises(ValueError):
        bridge._aplicar_bootstrap({"cli": "claude", "project_key": "RAG", "mode": "preview", "files": [_manifest(path, "x")]})


def test_rejects_modified_content_hash(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    bridge = _bridge(root)
    with pytest.raises(ValueError, match="huella"):
        bridge._aplicar_bootstrap({"cli": "claude", "project_key": "RAG", "mode": "preview", "files": [{"path": ".claude/agents/a.md", "content": "x", "sha256": "bad"}]})


def test_codex_escribe_solo_en_sus_carpetas(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    bridge = _bridge(root)
    archivos = [_manifest(".codex/agents/reviewer.toml", "x"), _manifest(".agents/skills/revisar/SKILL.md", "y")]
    result = bridge._aplicar_bootstrap({"cli": "codex", "project_key": "RAG", "mode": "apply", "files": archivos})
    assert [f["status"] for f in result["files"]] == ["written", "written"]
    assert (root / ".agents/skills/revisar/SKILL.md").read_text() == "y"

    for path in (".claude/agents/a.md", ".agent/rules/a.md", ".codex/config.toml", ".codex/agents/../config.toml"):
        with pytest.raises(ValueError):
            bridge._aplicar_bootstrap({"cli": "codex", "project_key": "RAG", "mode": "preview", "files": [_manifest(path, "x")]})
