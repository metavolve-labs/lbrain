"""File walker + frontmatter parser + markdown chunker + wikilink extractor."""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import tiktoken

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
# A line that declares this doc replaces another, e.g. "**Supersedes:** [[slug]]".
#
# The capture stops at a `·` or `|` separator (2026-07-30, anomaly A-422). It used
# to run to end-of-line, so the real corpus line
#     **Supersedes**: nothing · **Superseded by**: [[000-PRIORITY-ANOMALY-REGISTER]]
# captured the SECOND clause and recorded the edge BACKWARDS — the doc declaring
# itself replaced was registered as replacing the thing that replaced it. Verified
# in the live DB: the active Anomaly Register was sitting in superseded_slugs(),
# de-ranked by the redirect stub that points AT it. A supersession pointing the
# wrong way is worse than a missing one: it buries the live record and promotes
# the dead one.
SUPERSEDE_RE = re.compile(r"(?im)^[#>\s]*\**\s*supersedes\b[\s:*]*([^\n·|]*)")

# "nothing" / "none" / "n/a" is an author saying explicitly that this document
# replaces no other. Treating it as a value produced no edge by luck (no wikilink
# to find); being explicit costs one check and documents the intent.
_SUPERSEDE_EMPTY = {"", "nothing", "none", "n/a", "na", "-", "—"}
ENCODER = tiktoken.get_encoding("cl100k_base")


@dataclass
class Doc:
    path: Path
    rel_path: str
    title: str
    body: str
    metadata: dict
    wikilinks: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)  # slugs this doc replaces
    doc_hash: str = ""
    mtime: float = 0.0
    is_priority: bool = False
    doc_type: str = ""  # user/feedback/project/reference, from frontmatter

    @property
    def name_slug(self) -> str:
        """This document's identity for wikilink / supersession matching.

        `name:` frontmatter wins. Otherwise the filename stem — EXCEPT for a
        lair, where the payload is always `LAIR.md` and the identity is the
        containing directory. Deriving it from the filename made 164 of 167
        lairs share the slug "LAIR" (anomaly A-423). Must stay in step with
        search._basename_slug; they are two callers of one rule.
        """
        named = self.metadata.get("name")
        if named:
            return str(named)
        if self.path.stem == "LAIR" and self.path.parent.name:
            return self.path.parent.name
        return self.path.stem


@dataclass
class Chunk:
    doc_path: str
    chunk_idx: int
    text: str
    token_count: int
    chunk_hash: str
    context: str = ""  # doc macro-context prepended to embed/FTS text (not display)


# Pre-change snapshots. A backup is a COPY of a record that something else has
# since corrected — indexing it puts the superseded text in the results next to
# the fix that replaced it, competing on equal terms. Observed live 2026-07-28:
# a reconciliation left `backups-pre-apply-tranche2/` inside the lair tree and
# its copies ranked alongside the corrected originals for a compliance query.
_BACKUP_MARKERS = (
    "backups-pre-apply", "backups-pre-", "-pre-scrub", "/backups/", ".bak",
    "_ARCHIVED-", ".orig",
)


def is_backup_path(p: Path) -> bool:
    """True if this path is a pre-change snapshot rather than a live record."""
    s = p.as_posix()
    return any(m in s for m in _BACKUP_MARKERS)


def discover(roots: list[Path]) -> list[Path]:
    """Find indexable *.md under each root, refusing any path that resolves
    outside the root that offered it, and skipping pre-change backup trees.

    rglob does not descend into symlinked DIRECTORIES but it does yield
    symlinked FILES, and parse() then read_text()s them — so a cloned repo
    containing `docs/notes.md -> ../../../../.ssh/id_rsa` chose which of the
    user's files got indexed, embedded and served, under the repo's provenance.
    Git stores and checks out escaping symlinks verbatim. Verified end-to-end
    2026-07-28 (red-team finding 2). This is the single choke point for both
    `import` and `stale`.
    """
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            rr = root.resolve()
        except OSError:
            continue
        for p in sorted(root.rglob("*.md")):
            if is_backup_path(p):
                continue
            try:
                rp = p.resolve()
            except OSError:
                continue  # broken symlink / loop
            if not rp.is_file():
                continue
            if not rp.is_relative_to(rr):
                print(
                    f"[lbrain] SKIPPED {p}: resolves outside its source root "
                    f"({rp}). Symlinks may not escape the corpus.",
                    file=sys.stderr,
                )
                continue
            paths.append(p)
    return paths


def parse(path: Path, repo_root: Path | None = None) -> Doc:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        post = frontmatter.loads(text)
        body = post.content
        meta = dict(post.metadata)
    except Exception as e:
        # Malformed YAML silently strips ALL of a doc's metadata (type, supersedes,
        # name) — warn so the doc doesn't vanish from type filters / supersession
        # logic without anyone noticing.
        import sys

        print(f"[lbrain] WARNING: frontmatter parse failed for {path}: {e}", file=sys.stderr)
        body = text
        meta = {}

    title = meta.get("name") or _first_header(body) or path.stem
    wikilinks = list({m.group(1).strip() for m in WIKILINK_RE.finditer(body)})
    # Supersession — slugs this doc declares it replaces. Sources: frontmatter
    # `supersedes:` (string or list) and/or a body line `**Supersedes:** [[slug]]`.
    # Drives supersede-aware retrieval: the named docs are buried so the live
    # truth surfaces, while the originals stay indexed for provenance.
    supersedes: list[str] = []
    fm_sup = meta.get("supersedes")
    if isinstance(fm_sup, str):
        supersedes.extend(WIKILINK_RE.findall(fm_sup) or [fm_sup])
    elif isinstance(fm_sup, list):
        supersedes.extend(str(x) for x in fm_sup)
    for m in SUPERSEDE_RE.finditer(body):
        clause = m.group(1).strip().strip("*").strip()
        if clause.lower() in _SUPERSEDE_EMPTY:
            continue
        supersedes.extend(WIKILINK_RE.findall(clause))
    supersedes = sorted({s.strip() for s in supersedes if s and s.strip()})
    doc_type = ""
    if isinstance(meta.get("metadata"), dict):
        doc_type = str(meta["metadata"].get("type", "")) or ""
    elif "type" in meta:
        doc_type = str(meta["type"])

    rel = str(path.relative_to(repo_root)) if repo_root and repo_root in path.parents else str(path)
    # Split on BOTH separators: on Windows `rel` is "P5-X\000-PRIORITY-Y\LAIR.md",
    # so rel.split("/") returned the whole string as one element and the
    # 000-PRIORITY boost silently never fired — a ranking difference with no
    # error message, which is worse than a crash.
    is_priority = any(
        part.startswith("000-PRIORITY") for part in re.split(r"[\\/]", rel)
    )

    doc_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
    return Doc(
        path=path,
        rel_path=rel,
        title=str(title),
        body=body,
        metadata=meta,
        wikilinks=wikilinks,
        supersedes=supersedes,
        doc_hash=doc_hash,
        mtime=path.stat().st_mtime,
        is_priority=is_priority,
        doc_type=doc_type,
    )


def build_context(doc: Doc) -> str:
    """Doc-level macro-context for Contextual-Retrieval-style chunk prefixing.

    Cheap (no LLM): the doc title plus its frontmatter ``description`` (the
    one-line summary memory files already carry). This situates an isolated
    chunk inside its parent document so deep chunks that never restate the
    doc's subject still embed/match under it.
    """
    parts = [doc.title.strip()]
    desc = doc.metadata.get("description")
    if isinstance(desc, str) and desc.strip():
        parts.append(desc.strip())
    return " — ".join(parts)


def chunk(
    doc: Doc, max_tokens: int = 512, overlap: int = 64, contextualize: bool = False
) -> list[Chunk]:
    """Header-aware chunking. Splits on H1/H2 boundaries, then packs to max_tokens.

    When ``contextualize`` is set, every chunk carries the doc's macro-context
    (stored separately; prepended to embed/FTS text, never to the display text).
    """
    ctx = build_context(doc) if contextualize else ""
    sections = _split_on_headers(doc.body)
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    idx = 0
    for sec in sections:
        sec_tokens = len(ENCODER.encode(sec))
        if buf_tokens + sec_tokens <= max_tokens:
            buf.append(sec)
            buf_tokens += sec_tokens
        else:
            if buf:
                chunks.append(_make_chunk(doc, idx, "\n".join(buf), buf_tokens, ctx))
                idx += 1
            if sec_tokens <= max_tokens:
                buf = [sec]
                buf_tokens = sec_tokens
            else:
                # section bigger than max_tokens — sliding window
                tokens = ENCODER.encode(sec)
                step = max_tokens - overlap
                for start in range(0, len(tokens), step):
                    piece = ENCODER.decode(tokens[start : start + max_tokens])
                    chunks.append(_make_chunk(doc, idx, piece, min(max_tokens, len(tokens) - start), ctx))
                    idx += 1
                buf = []
                buf_tokens = 0
    if buf:
        chunks.append(_make_chunk(doc, idx, "\n".join(buf), buf_tokens, ctx))
    return chunks


def _split_on_headers(body: str) -> list[str]:
    """Split body on H1/H2 boundaries; each section keeps its header."""
    matches = list(re.finditer(r"^(#{1,2})\s+.+$", body, re.MULTILINE))
    if not matches:
        return [body]
    sections = []
    if matches[0].start() > 0:
        sections.append(body[: matches[0].start()].rstrip())
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append(body[m.start() : end].rstrip())
    return [s for s in sections if s.strip()]


def _make_chunk(doc: Doc, idx: int, text: str, tokens: int, context: str = "") -> Chunk:
    # No context → legacy hash sha1(text) byte-for-byte, so flipping the flag
    # OFF leaves change-detection identical to pre-context builds. With context,
    # fold it in so flipping ON is correctly seen as a change.
    payload = f"{context}\x00{text}" if context else text
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return Chunk(
        doc_path=doc.rel_path, chunk_idx=idx, text=text, token_count=tokens,
        chunk_hash=h, context=context,
    )


def _first_header(body: str) -> str | None:
    m = HEADER_RE.search(body)
    return m.group(2).strip() if m else None
