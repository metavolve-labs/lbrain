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


class TestTheNameIsAWritePath:
    """`install()` writes to `dest/<name>/`, and the name comes from module.toml.

    `validate()` calls itself "every reason this module must not ship" and
    checked every field except the one used to build the write path. A traversal
    escaped `dest`; an absolute name ignored it entirely, because
    `Path('/dest') / '/abs' == Path('/abs')`. The validator blocked symlinks,
    exec bits and executable suffixes, and left the destination itself open.
    """

    def _named(self, tmp_path, name):
        return _mod(tmp_path, manifest=MANIFEST.replace('name = "t"', f'name = "{name}"')
                    if 'name = "t"' in MANIFEST else MANIFEST)

    def test_a_traversal_name_does_not_validate(self, tmp_path):
        root = _mod(tmp_path)
        mt = (root / "module.toml").read_text(encoding="utf-8")
        (root / "module.toml").write_text(
            mt.replace(mt.split("name = ")[1].split("\n")[0],
                       '"../../../../../../tmp/pwned"'), encoding="utf-8")
        assert any("safe path segment" in p for p in modules.validate(root))

    def test_an_absolute_name_does_not_validate(self, tmp_path):
        root = _mod(tmp_path)
        mt = (root / "module.toml").read_text(encoding="utf-8")
        (root / "module.toml").write_text(
            mt.replace(mt.split("name = ")[1].split("\n")[0],
                       '"/tmp/lbrain-abs-pwn"'), encoding="utf-8")
        assert any("safe path segment" in p for p in modules.validate(root))

    def test_an_ordinary_name_still_validates(self, tmp_path):
        """The guard must not refuse the names real modules use."""
        assert modules.validate(_mod(tmp_path)) == []


class TestTheDestinationIsNotTrusted:
    def test_a_dangling_symlink_is_refused_not_written_through(self, tmp_path):
        """`out.exists()` follows links, so a DANGLING symlink is not `exists()`.

        It passed the never-overwrite guard and `write_text` created the file at
        the link's target — outside `dest`, while the CLI reported the in-dest
        path. A live symlink hit the opposite failure and was skipped as "already
        there". Wrong in both directions, and `validate()` rejects symlinks
        *inside* a module for exactly this reason.
        """
        m = modules.load(_mod(tmp_path))
        dest = tmp_path / "corpus"
        (dest / m.name / "questions").mkdir(parents=True)
        victim = tmp_path / "VICTIM.md"
        (dest / m.name / "questions" / "001-q.md").symlink_to(victim)

        with pytest.raises(ValueError, match="symlink"):
            modules.install(m, dest)
        assert not victim.exists(), "install wrote through the symlink"

    def test_nothing_is_written_before_the_symlink_is_detected(self, tmp_path):
        """Pre-scan, not fail-midway: a refusal must not leave a half-copy."""
        root = _mod(tmp_path)
        (root / "questions" / "002-q.md").write_text(GOOD_RECORD, encoding="utf-8")
        m = modules.load(root)
        dest = tmp_path / "corpus"
        (dest / m.name / "questions").mkdir(parents=True)
        (dest / m.name / "questions" / "002-q.md").symlink_to(tmp_path / "VICTIM2.md")

        with pytest.raises(ValueError, match="symlink"):
            modules.install(m, dest)
        assert not (dest / m.name / "questions" / "001-q.md").exists()


class TestValidationIsNotAFunctionOfWhatBrowsedTheDirectory:
    def test_a_ds_store_does_not_block_a_module(self, tmp_path):
        """The Finder writes `.DS_Store` into any directory it browses, and the
        maintainer is on macOS. `Path.suffix` is `''` for it, which is in no
        allowlist — so browsing a folder broke `lbrain module add`."""
        root = _mod(tmp_path)
        (root / ".DS_Store").write_bytes(b"\x00\x01")
        assert modules.validate(root) == []

    def test_a_license_file_does_not_block_a_module(self, tmp_path):
        root = _mod(tmp_path)
        (root / "LICENSE").write_text("BSD-3-Clause\n", encoding="utf-8")
        assert modules.validate(root) == []

    def test_a_git_directory_does_not_block_a_module(self, tmp_path):
        root = _mod(tmp_path)
        (root / ".git" / "objects").mkdir(parents=True)
        (root / ".git" / "objects" / "deadbeef").write_bytes(b"\x78\x01")
        assert modules.validate(root) == []

    def test_an_unexpected_shipping_type_is_still_rejected(self, tmp_path):
        """The relaxation must not disarm the rule it relaxes."""
        root = _mod(tmp_path)
        (root / "data.json").write_text("{}", encoding="utf-8")
        assert any("may ship" in p for p in modules.validate(root))


class TestTheReadmeExemptionIsScopedToTheRoot:
    def test_a_readme_inside_questions_obeys_the_record_rules(self, tmp_path):
        """`install()` copies `questions/*.md`, so `questions/README.md` ships as
        a question record. A filename-only exemption made the format's centrepiece
        prohibition — which DESIGN-modules.md calls "enforced mechanically because
        an advisory rule is one an exporter forgets" — opt-out by renaming a file,
        in the exact directory the installer ships from.
        """
        root = _mod(tmp_path)
        (root / "questions" / "README.md").write_text(
            "---\nevidence: observed\ntype: project\n---\n"
            "# Vendor X is SOC-2 certified\n\nWe audited it ourselves.\n",
            encoding="utf-8")
        problems = modules.validate(root)
        assert any("observed" in p for p in problems), problems

    def test_the_root_readme_is_still_exempt(self, tmp_path):
        """Prose about the module is not a record in it."""
        root = _mod(tmp_path)
        (root / "README.md").write_text("# About\n\nWhat this module asks.\n", encoding="utf-8")
        assert modules.validate(root) == []
