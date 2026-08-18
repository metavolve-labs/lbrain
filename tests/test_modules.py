"""Modules — role scaffolding shipped as data.

The rules under test are the ones that keep a module safe inside a source-cited
engine. A module is content a stranger wrote, installed into a corpus whose
records this engine serves dated, attributed and `binds`. Two properties carry
that: it cannot run, and it cannot claim to have seen anything.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from lbrain import modules

MANIFEST = """\
[module]
name = "t"
title = "T"
version = "0.1.0"
authored = "2026-08-17"
description = "d"
"""

GOOD_RECORD = "---\ndate: 2026-08-17\nevidence: synthesized\n---\n\n# Q\n\nAsk this.\n"


def _mod(tmp_path: Path, manifest: str = MANIFEST, record: str = GOOD_RECORD) -> Path:
    root = tmp_path / "t"
    (root / "questions").mkdir(parents=True)
    (root / "module.toml").write_text(manifest, encoding="utf-8")
    if record is not None:
        (root / "questions" / "001-q.md").write_text(record, encoding="utf-8")
    return root


class TestBundled:
    def test_role_continuity_loads_and_validates(self):
        m = modules.get("role-continuity")
        assert m.questions, "a module with no questions has nothing to offer"
        assert modules.validate(m.root) == []

    def test_every_bundled_module_validates(self):
        """The rules apply hardest to what we ship ourselves."""
        for m in modules.discover():
            assert modules.validate(m.root) == [], m.name

    def test_no_bundled_record_claims_to_have_observed_anything(self):
        import frontmatter

        from lbrain import grading

        for m in modules.discover():
            for q in m.questions:
                ev = grading.parse_evidence(dict(frontmatter.loads(
                    q.read_text(encoding="utf-8")).metadata), q)
                assert ev != grading.OBSERVED, q

    def test_bundled_content_names_nothing_internal(self):
        """It ships to strangers — same standard as the framework docs (A-408)."""
        import re

        bad = re.compile(
            r"metavolve|golden.?codex|artiswa|aeternum|atmtad|curator@|"
            r"\bTad\b|/mnt/[a-z]/|C:\\\\Users",
            re.I,
        )
        for m in modules.discover():
            for f in m.root.rglob("*"):
                if f.is_file():
                    hits = bad.findall(f.read_text(encoding="utf-8", errors="replace"))
                    assert not hits, f"{f} leaks {hits}"


class TestManifest:
    def test_missing_manifest_says_so(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(ValueError, match="module.toml"):
            modules.load(tmp_path / "empty")

    def test_missing_keys_are_all_named_at_once(self, tmp_path):
        root = _mod(tmp_path, manifest='[module]\nname = "t"\n')
        with pytest.raises(ValueError) as e:
            modules.load(root)
        for k in ("title", "version", "authored", "description"):
            assert k in str(e.value)

    def test_unparseable_toml_is_a_reason_not_a_traceback(self, tmp_path):
        root = _mod(tmp_path, manifest="[module\nname =")
        with pytest.raises(ValueError, match="readable TOML"):
            modules.load(root)

    def test_non_iso_authored_date_is_a_problem(self, tmp_path):
        root = _mod(tmp_path, manifest=MANIFEST.replace('"2026-08-17"', '"August 2026"'))
        assert any("authored" in p for p in modules.validate(root))

    def test_get_names_what_is_available(self):
        with pytest.raises(ValueError, match="role-continuity"):
            modules.get("no-such-module")


class TestAModuleMayNotClaimToHaveSeenAnything:
    def test_observed_is_rejected(self, tmp_path):
        """The centerpiece rule. Its author was never inside your company."""
        root = _mod(tmp_path, record=GOOD_RECORD.replace("synthesized", "observed"))
        problems = modules.validate(root)
        assert any("observed" in p for p in problems), problems

    def test_sourced_and_synthesized_are_fine(self, tmp_path):
        for ev in ("sourced", "synthesized"):
            root = _mod(tmp_path / ev, record=GOOD_RECORD.replace("synthesized", ev))
            assert modules.validate(root) == [], ev

    def test_ungraded_record_is_rejected(self, tmp_path):
        root = _mod(tmp_path, record="---\ndate: 2026-08-17\n---\n\n# Q\n\nAsk.\n")
        assert any("evidence" in p for p in modules.validate(root))

    def test_undated_record_is_rejected(self, tmp_path):
        """A module is COPIED by definition; an mtime does not survive that."""
        root = _mod(tmp_path, record="---\nevidence: synthesized\n---\n\n# Q\n\nAsk.\n")
        assert any("date" in p for p in modules.validate(root))

    def test_readme_is_exempt_from_record_rules(self, tmp_path):
        root = _mod(tmp_path)
        (root / "README.md").write_text("# T\n\nProse.\n", encoding="utf-8")
        assert modules.validate(root) == []

    def test_a_module_with_no_questions_is_rejected(self, tmp_path):
        root = tmp_path / "t"
        root.mkdir()
        (root / "module.toml").write_text(MANIFEST, encoding="utf-8")
        assert any("questions" in p for p in modules.validate(root))


class TestAModuleCannotRun:
    def test_script_is_rejected(self, tmp_path):
        root = _mod(tmp_path)
        (root / "setup.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        assert any("executable content" in p for p in modules.validate(root))

    def test_python_is_rejected(self, tmp_path):
        root = _mod(tmp_path)
        (root / "hook.py").write_text("print(1)\n", encoding="utf-8")
        assert any("executable content" in p for p in modules.validate(root))

    def test_execute_bit_on_markdown_is_rejected(self, tmp_path):
        """A .md file someone chmod +x'd is still a thing someone tried."""
        root = _mod(tmp_path)
        f = root / "questions" / "001-q.md"
        os.chmod(f, 0o755)
        assert any("execute bit" in p for p in modules.validate(root))

    def test_symlink_is_rejected(self, tmp_path):
        root = _mod(tmp_path)
        outside = tmp_path / "outside.md"
        outside.write_text("---\ndate: 2026-08-17\nevidence: sourced\n---\n\n# X\n", encoding="utf-8")
        (root / "questions" / "link.md").symlink_to(outside)
        assert any("symlink" in p for p in modules.validate(root))

    def test_unexpected_file_type_is_rejected(self, tmp_path):
        root = _mod(tmp_path)
        (root / "data.json").write_text("{}", encoding="utf-8")
        assert any("may ship" in p for p in modules.validate(root))


class TestInstall:
    def test_writes_the_questions(self, tmp_path):
        m = modules.get("role-continuity")
        written = modules.install(m, tmp_path)
        assert written
        assert all(p.exists() for p in written)

    def test_never_overwrites_an_answered_file(self, tmp_path):
        """The module exists to produce the user's records — replacing one is
        deleting exactly the thing it was for."""
        m = modules.get("role-continuity")
        modules.install(m, tmp_path)
        target = (tmp_path / m.name / "questions" / m.questions[0].name)
        target.write_text("MY ANSWER", encoding="utf-8")
        assert modules.install(m, tmp_path) == []
        assert target.read_text(encoding="utf-8") == "MY ANSWER"

    def test_refuses_an_invalid_module(self, tmp_path):
        root = _mod(tmp_path, record=GOOD_RECORD.replace("synthesized", "observed"))
        mod = modules.load(root)
        with pytest.raises(ValueError, match="does not validate"):
            modules.install(mod, tmp_path / "dest")


class TestPackaging:
    def test_modules_are_present_in_a_built_wheel(self):
        """The framework's bug was never 'files missing' — it was 'nothing
        packages them' (A-408). Same failure is available here."""
        dists = sorted(Path(__file__).resolve().parents[1].glob("dist/*.whl"))
        if not dists:
            pytest.skip("no wheel built")
        names = zipfile.ZipFile(dists[-1]).namelist()
        assert any(n.startswith("lbrain/modules/") and n.endswith("module.toml")
                   for n in names), "wheel ships no module manifest"
        assert any("/questions/" in n and n.endswith(".md") for n in names), \
            "wheel ships no module questions"
