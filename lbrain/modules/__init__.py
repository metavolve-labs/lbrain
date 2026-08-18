"""Modules — role scaffolding for a corpus, shipped as data.

A module is a FRAME, not a payload. It does not tell you how your job works; it
tells your agent what to ask, what record types the role produces, and how fast
each of them goes stale. The records it ships are questions. The records that
matter are the ones you write answering them.

That is a deliberate inversion, and it is what makes a module safe inside a
source-cited engine. A declarative module asserts things about YOUR organisation
that its author never saw, and this engine would serve those assertions dated,
attributed and `binds` — its own credibility laundering someone else's guess. A
question asserts nothing. It cannot be wrong about a company it has never been
inside, and answering it produces a record that is `observed` by the person who
answered.

So the validator's central rule is not stylistic:

    A module may not ship a record graded `observed`.

Nobody authoring a module has witnessed anything at your company. A module that
claims otherwise is making the exact error the two-axis grade exists to catch
(lbrain/grading.py), and the check is mechanical rather than advisory because an
advisory rule is one an exporter forgets.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

from .. import grading

BUNDLED = Path(__file__).parent

MANIFEST = "module.toml"
REQUIRED = ("name", "title", "version", "authored", "description")

# Data only. A module is content a stranger wrote; if it can also run, "download
# a module" becomes "run a stranger's code", and the distribution channel becomes
# a supply chain. Enforced by extension AND by mode, because a .md file with the
# execute bit set is still a thing someone tried.
EXECUTABLE_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".py", ".pyc", ".js", ".mjs", ".rb", ".pl", ".ps1",
    ".bat", ".cmd", ".exe", ".dylib", ".so", ".command",
}
ALLOWED_SUFFIXES = {".md", ".toml"}

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Module:
    name: str
    title: str
    version: str
    authored: str
    description: str
    root: Path
    doc_types: list[str] = field(default_factory=list)
    lairs: list[str] = field(default_factory=list)
    staleness_days: int = 0

    @property
    def questions(self) -> list[Path]:
        q = self.root / "questions"
        return sorted(q.glob("*.md")) if q.is_dir() else []


def load(root: Path) -> Module:
    """Read a module directory. Raises ValueError with a reason it can be fixed by."""
    root = Path(root)
    man = root / MANIFEST
    if not man.is_file():
        raise ValueError(f"{root} has no {MANIFEST}")
    try:
        data = tomllib.loads(man.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"{man} is not readable TOML: {e}") from e
    meta = data.get("module") or {}
    missing = [k for k in REQUIRED if not meta.get(k)]
    if missing:
        raise ValueError(f"{man} [module] is missing: {', '.join(missing)}")
    corpus = data.get("corpus") or {}
    stale = data.get("staleness") or {}
    return Module(
        name=str(meta["name"]),
        title=str(meta["title"]),
        version=str(meta["version"]),
        authored=str(meta["authored"]),
        description=str(meta["description"]),
        root=root,
        doc_types=[str(x) for x in corpus.get("doc_types", [])],
        lairs=[str(x) for x in corpus.get("lairs", [])],
        staleness_days=int(stale.get("default_days", 0) or 0),
    )


def discover(extra: Path | None = None) -> list[Module]:
    """Bundled modules, plus any under ``extra``. Unreadable ones warn, not crash."""
    roots = [p for p in BUNDLED.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]
    if extra and Path(extra).is_dir():
        roots += [
            p for p in sorted(Path(extra).iterdir())
            if p.is_dir() and (p / MANIFEST).is_file()
        ]
    out = []
    for r in sorted(roots):
        try:
            out.append(load(r))
        except ValueError as e:
            print(f"[lbrain] WARNING: skipping module at {r}: {e}", file=sys.stderr)
    return out


def get(name: str, extra: Path | None = None) -> Module:
    for m in discover(extra):
        if m.name == name:
            return m
    have = ", ".join(m.name for m in discover(extra)) or "none"
    raise ValueError(f"no module named {name!r}; have: {have}")


def validate(root: Path) -> list[str]:
    """Every reason this module must not ship. Empty list = clean.

    Returns problems rather than raising so an author sees all of them at once;
    fixing one error per run is how a validator becomes a thing people route
    around.
    """
    root = Path(root)
    problems: list[str] = []
    try:
        mod = load(root)
    except ValueError as e:
        return [str(e)]

    if not _ISO.match(mod.authored):
        problems.append(f"[module] authored = {mod.authored!r} is not YYYY-MM-DD")
    if not mod.questions:
        problems.append(
            "no questions/ records — a module whose records assert instead of ask "
            "is the shape this format exists to prevent"
        )

    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_symlink():
            problems.append(f"{rel}: symlink — a module is data, and a link leaves it")
            continue
        if p.is_dir():
            continue
        if p.suffix.lower() in EXECUTABLE_SUFFIXES:
            problems.append(f"{rel}: executable content — a module is data, not code")
            continue
        if p.suffix.lower() not in ALLOWED_SUFFIXES:
            problems.append(f"{rel}: only {'/'.join(sorted(ALLOWED_SUFFIXES))} may ship")
            continue
        if p.stat().st_mode & 0o111:
            problems.append(f"{rel}: has the execute bit set")
        if p.suffix.lower() == ".md":
            problems.extend(f"{rel}: {w}" for w in _check_record(p))
    return problems


def _check_record(path: Path) -> list[str]:
    """Grading and dating rules for one shipped record."""
    import frontmatter

    out: list[str] = []
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        meta = dict(post.metadata)
    except Exception as e:
        return [f"frontmatter does not parse ({e})"]

    if path.name.lower() in {"readme.md"}:
        return out

    date = str(meta.get("date", "")).strip()
    if not _ISO.match(date):
        out.append(
            "no frontmatter `date:` — a module is COPIED by definition, and a claim "
            "date that lives only in an mtime does not survive the copy"
        )

    ev = grading.parse_evidence(meta, path)
    if not grading.is_graded(ev):
        out.append(
            "no frontmatter `evidence:` — an ungraded record shipped to a stranger "
            f"grades {grading.CRED_UNGRADED} and says nothing about how it is known"
        )
    elif ev == grading.OBSERVED:
        out.append(
            "declares `evidence: observed`, which a module may never do — its author "
            "has witnessed nothing at the organisation that installs it. Use "
            f"`{grading.SOURCED}` for a citable claim or `{grading.SYNTHESIZED}` for a "
            "reasoned one"
        )
    return out


def install(mod: Module, dest: Path) -> list[Path]:
    """Copy a module's records under ``dest``/<name>/. Refuses an invalid module.

    Never overwrites. A module is scaffolding for a corpus that is about to grow
    its own records; silently replacing a file the user has since answered into
    would delete exactly the thing the module exists to produce.
    """
    problems = validate(mod.root)
    if problems:
        raise ValueError(
            f"module {mod.name!r} does not validate:\n  - " + "\n  - ".join(problems)
        )
    target = Path(dest) / mod.name
    written: list[Path] = []
    for src in [*mod.questions, *(p for p in [mod.root / "README.md"] if p.is_file())]:
        rel = src.relative_to(mod.root)
        out = target / rel
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(out)
    return written
