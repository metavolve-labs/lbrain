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

# SUP-05: a supersession declaration is a real edge only when it is the author's
# OWN column-0 statement — not a line QUOTED (`>`), INDENTED (code), or FENCED
# (```). The old regex allowed `[#>\s]*` before "supersedes", so an incident
# review quoting the Anomaly Register's own Supersedes line minted an edge that
# de-ranked the live register. The regex cannot see fences, so the scan is
# line-aware: it tracks fence state and rejects non-column-0 lines.
_SUPERSEDE_LINE_RE = re.compile(r"(?i)^\*{0,2}\s*supersedes\b[\s:*]*([^\n·|]*)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# C2-12 (case A): the indent/quote/fence rejection in _body_supersedes must run on
# the body with LEADING WHITESPACE PRESERVED. python-frontmatter's `.content`
# strips the leading line's whitespace, so an indented `Supersedes:` that is the
# FIRST body line arrived at _body_supersedes already de-indented — the NFKC check
# was dead code on that path and the line minted an edge (verified through parse()
# by CSO blind re-verify: RED == GREEN on a3d35a1). We strip ONLY the frontmatter
# block from the raw source, never the body's own indentation.
_FRONTMATTER_BLOCK_RE = re.compile(r"^(?:﻿)?---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)


def _raw_body(text: str) -> str:
    """The document body verbatim (frontmatter block removed, indentation intact)."""
    m = _FRONTMATTER_BLOCK_RE.match(text)
    return text[m.end():] if m else text


def _body_supersedes(body: str) -> list[str]:
    """Slugs a doc declares it replaces, from its OWN column-0 body lines only.

    Rejects quoted / indented / fenced lines (SUP-05). Captures both the wikilink
    form `**Supersedes:** [[X]]` and the bare-slug form `**Supersedes:** X` (L5),
    honouring the empty-guard (`nothing`/`none`/`-`)."""
    import unicodedata
    out: list[str] = []
    in_fence = False
    for line in body.splitlines():
        line_norm = unicodedata.normalize("NFKC", line)
        if _FENCE_RE.match(line_norm):
            in_fence = not in_fence
            continue
        if in_fence or line_norm[:1] in (" ", "\t", ">", "#"):
            continue  # fenced, indented (code), quoted, or a heading — not a declaration
        m = _SUPERSEDE_LINE_RE.match(line_norm)
        if not m:
            continue
        clause = m.group(1).strip().strip("*").strip()
        if clause.lower() in _SUPERSEDE_EMPTY:
            continue
        links = WIKILINK_RE.findall(clause)
        raw_slugs = links or [clause]
        for s in raw_slugs:
            s_clean = s.strip().lower()
            if s_clean not in _SUPERSEDE_EMPTY:
                out.append(s)
    return out

# "nothing" / "none" / "n/a" is an author saying explicitly that this document
# replaces no other. Treating it as a value produced no edge by luck (no wikilink
# to find); being explicit costs one check and documents the intent.
_SUPERSEDE_EMPTY = {"", "nothing", "none", "n/a", "na", "-", "—"}


def _sup_slugs(raw: str) -> list[str]:
    """Wikilink targets in `raw` (or the bare clause), each empty-guarded on the
    EXTRACTED value. C2-12: `[[nothing]]` extracts to 'nothing', which the
    empty-guard must catch — the raw-clause guard let a wikilink-wrapped empty
    target through ('[[nothing]]' != 'nothing') and minted a spurious edge."""
    out: list[str] = []
    for slug in (WIKILINK_RE.findall(raw) or [raw.strip()]):
        s = slug.strip()
        if s and s.lower() not in _SUPERSEDE_EMPTY:
            out.append(s)
    return out
# OFF-13: load the tokenizer LAZILY, not at module scope. `get_encoding` fetches
# the BPE vocabulary from a CDN on a cold `~/.tiktoken`; doing it at import made a
# "local-first" engine unimportable offline. The fetch now happens on first
# actual USE (chunking needs it anyway), so import — and every read path that
# doesn't chunk — works with no network. `index.ENCODER` still resolves, lazily,
# via module __getattr__ for any external caller.
_ENCODER = None


def _encoder():
    """The cl100k_base tokenizer, loaded once on first use."""
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def __getattr__(name):
    if name == "ENCODER":
        return _encoder()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class Doc:
    path: Path
    rel_path: str
    title: str
    body: str
    metadata: dict
    wikilinks: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)  # slugs this doc replaces
    claims: list = field(default_factory=list)  # claim-span lifecycle records (dual-view)
    doc_hash: str = ""
    mtime: float = 0.0
    is_priority: bool = False
    doc_type: str = ""  # user/feedback/project/reference/belief, from frontmatter
    # False when the YAML block failed to parse. Callers that DERIVE state from
    # frontmatter must distinguish "this document says nothing" from "this
    # document's metadata is unreadable" — they are opposite facts, and treating
    # the second as the first silently discards state (see cli._project_belief).
    metadata_ok: bool = True
    # Disclosure class (lbrain/disclosure.py): artifact | proposal | private.
    # '' = unclassified, which every blinding mode WITHHOLDS. An unrecognised
    # value is normalised to '' rather than trusted: a class nobody defined
    # cannot be proven safe to disclose.
    disclosure: str = ""
    # Frontmatter `date:`, normalised to ISO. The PORTABLE claim date — it rides
    # inside the file, so it survives a copy/clone/mtime reset. Carried as a doc
    # field because `parse()` strips the frontmatter out of `body`, so nothing
    # downstream of chunking can recover it from text.
    claim_date: str = ""
    # Evidence class (lbrain/grading.py): observed | sourced | synthesized.
    # '' = UNGRADED, which grades Admiralty 6 ("truth cannot be judged") and is
    # never promoted to 3. The credibility axis of the two-axis grade; the
    # source axis is DERIVED and never read from a file.
    evidence: str = ""

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
    # Ancestor headings ABOVE this chunk's own heading, outermost first (" > ").
    # Structural provenance, always populated — not an embedding option like
    # `context`. A chunk's own heading is already in `text`; what was missing is
    # everything the split threw away above it (A-513).
    heading_path: str = ""


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
    metadata_ok = True
    try:
        post = frontmatter.loads(text)
        body = post.content
        meta = dict(post.metadata)
    except Exception as e:
        metadata_ok = False
        # Malformed YAML silently strips ALL of a doc's metadata (type, supersedes,
        # name) — warn so the doc doesn't vanish from type filters / supersession
        # logic without anyone noticing.
        # NOTE: no local `import sys` here. It bound `sys` as a function-local for
        # the WHOLE of parse(), so the module-level import became invisible and any
        # later sys.stderr use raised UnboundLocalError — on the error path only,
        # which is the path least likely to be exercised.
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

    def _flatten_supersedes(val) -> list[str]:
        if isinstance(val, list):
            out = []
            for x in val:
                out.extend(_flatten_supersedes(x))
            return out
        elif isinstance(val, str):
            return [val]
        elif val is None:
            return []
        else:
            return [str(val)]

    for xs in _flatten_supersedes(fm_sup):
        xs_strip = xs.strip()
        if xs_strip.lower() not in _SUPERSEDE_EMPTY:
            extracted = WIKILINK_RE.findall(xs_strip) or [xs_strip]
            for s in extracted:
                if s.strip().lower() not in _SUPERSEDE_EMPTY:
                    supersedes.append(s)
    # C2-12 (case A): scan the RAW body — frontmatter's `.content` (`body`) strips
    # a leading indented line, defeating the indent/quote/fence rejection.
    supersedes.extend(_body_supersedes(_raw_body(text)))
    supersedes = sorted({s.strip() for s in supersedes if s and s.strip()})
    doc_type = ""
    if isinstance(meta.get("metadata"), dict):
        doc_type = str(meta["metadata"].get("type", "")) or ""
    elif "type" in meta:
        doc_type = str(meta["type"])

    # Disclosure class. Accepted at the top level or nested under `metadata:`,
    # matching how `type:` is already written in this corpus. Unknown values fall
    # to '' (withheld under blinding) rather than being passed through — an
    # author's typo must not mint a class the filter has never heard of and
    # therefore cannot reason about.
    from .disclosure import CLASSES as _DISCLOSURE_CLASSES

    raw_disclosure = ""
    if isinstance(meta.get("metadata"), dict) and meta["metadata"].get("disclosure"):
        raw_disclosure = str(meta["metadata"]["disclosure"])
    elif meta.get("disclosure"):
        raw_disclosure = str(meta["disclosure"])
    disclosure = raw_disclosure.strip().lower()
    if disclosure and disclosure not in _DISCLOSURE_CLASSES:
        print(
            f"[lbrain] WARNING: {path} declares disclosure: {raw_disclosure!r}, which is not "
            f"one of {'/'.join(_DISCLOSURE_CLASSES)} — treating it as UNCLASSIFIED "
            "(withheld under any blinding mode).",
            file=sys.stderr,
        )
        disclosure = ""

    # Evidence class. Same accept-or-warn shape as disclosure directly above,
    # and for the same reason: an unrecognised grade must not reach the ranker.
    from .grading import parse_evidence as _parse_evidence

    evidence = _parse_evidence(meta, path)

    # The frontmatter claim date, resolved HERE because this is the last place
    # the frontmatter exists — `body` below has it stripped.
    from .staleness import normalize_claim_date as _norm_date

    # Nested `metadata: date:` first, then top level — the SAME precedence
    # `parse_evidence` six lines above uses, and for the reason its docstring
    # already gives: it matches "how `type:` and `disclosure:` are already
    # written in this corpus." Reading only the top level meant a record written
    # in the house nested form got its GRADE through and lost its DATE, so it
    # still reaged to its import day — the headline fix missing the half of the
    # corpus that follows the house convention. The in-text `_FM_DATE` regex
    # cannot rescue it either: it is anchored to a column-0 `^date:`.
    _nested = meta.get("metadata")
    fm_claim_date = ""
    if isinstance(_nested, dict) and _nested.get("date") is not None:
        fm_claim_date = _norm_date(_nested.get("date"))
    if not fm_claim_date:
        fm_claim_date = _norm_date(meta.get("date"))
    if not fm_claim_date:
        # `generated:` is the house key for synthesized records (`lbrain consolidate`).
        # Without this tier a synthesis carried NO in-content date, so its age came only
        # from mtime — and any later revision reaged it. 2026-08-22: eleven abstractions
        # were corrected in place (false "under review at TMLR" claims) and every one
        # jumped from `generated 2026-07-11` to `generated 2026-08-22`, making the
        # STALEST records in the corpus look like the freshest. A correction is not a
        # re-synthesis. The date has to live in the content, like every other tier here.
        fm_claim_date = _norm_date(meta.get("generated"))

    rel = str(path.relative_to(repo_root)) if repo_root and repo_root in path.parents else str(path)
    # Split on BOTH separators: on Windows `rel` is "TOPIC\000-PRIORITY-Y\LAIR.md",
    # so rel.split("/") returned the whole string as one element and the
    # 000-PRIORITY boost silently never fired — a ranking difference with no
    # error message, which is worse than a crash.
    is_priority = any(
        part.startswith("000-PRIORITY") for part in re.split(r"[\\/]", rel)
    )

    # Claim-span dual-view (DR panel 2026-08-30): a `claims:` frontmatter list retires
    # specific claims INSIDE a living document. Each {text, status, valid_to}. current_only
    # retrieval drops any CHUNK whose text contains a closed claim's text — so a fresh
    # file's stale span is retired without retiring the whole file (the grain mismatch).
    # Text-matching, not char offsets, so an edit that shifts positions can't silently
    # mis-target (the offset-recompute fragility the panel flagged).
    claims = []
    _raw_claims = meta.get("claims")
    if isinstance(_raw_claims, list):
        for _c in _raw_claims:
            if not isinstance(_c, dict):
                continue
            _t = str(_c.get("text", "")).strip()
            if not _t:
                continue
            claims.append({
                "text": _t,
                "status": str(_c.get("status", "current")).strip().lower(),
                "valid_to": _norm_date(_c.get("valid_to")) if _c.get("valid_to") else "",
            })

    doc_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
    return Doc(
        path=path,
        rel_path=rel,
        title=str(title),
        body=body,
        metadata=meta,
        wikilinks=wikilinks,
        supersedes=supersedes,
        claims=claims,
        doc_hash=doc_hash,
        mtime=path.stat().st_mtime,
        is_priority=is_priority,
        doc_type=doc_type,
        metadata_ok=metadata_ok,
        disclosure=disclosure,
        evidence=evidence,
        claim_date=fm_claim_date,
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
    buf_path = ""  # heading path of the FIRST section in the buffer
    idx = 0
    for sec, hpath in sections:
        sec_tokens = len(_encoder().encode(sec))
        if buf_tokens + sec_tokens <= max_tokens:
            if not buf:
                buf_path = hpath
            buf.append(sec)
            buf_tokens += sec_tokens
        else:
            if buf:
                chunks.append(
                    _make_chunk(doc, idx, "\n".join(buf), buf_tokens, ctx, buf_path)
                )
                idx += 1
            if sec_tokens <= max_tokens:
                buf = [sec]
                buf_tokens = sec_tokens
                buf_path = hpath
            else:
                # section bigger than max_tokens — line-aware, table-aware window
                pieces, idx = _window_section(
                    doc, sec, max_tokens, overlap, ctx, idx, hpath
                )
                chunks.extend(pieces)
                buf = []
                buf_tokens = 0
                buf_path = ""
    if buf:
        chunks.append(_make_chunk(doc, idx, "\n".join(buf), buf_tokens, ctx, buf_path))
    return chunks


# Bump whenever chunk BOUNDARIES change. Import short-circuits on the body hash
# (`existing_hash == doc.doc_hash`), which is correct for content but blind to the
# algorithm: shipping A-412's table-aware windowing left every existing corpus on
# the old boundaries, silently, with no way for a user to notice their index was
# built by code they no longer run. `doctor` already fingerprints the EMBEDDING
# provider/model/dim against stored values; this applies the same idea to the
# chunker, which had no version identity at all.
#
# 1 -> 2 : line-aware, table-aware _window_section (A-412, 2026-08-01)
# 2 -> 3 : heading ancestry (A-513, 2026-08-03). Boundaries are UNCHANGED — this
#          is the first bump that isn't about where the cuts land. It is here
#          because the rule's PURPOSE is "the index must not silently disagree
#          with the code that built it", and a chunk that gains ancestry embeds
#          and matches differently. Cost, stated plainly: every corpus with H2
#          sections under an H1 re-chunks and RE-EMBEDS on next import. Flat and
#          single-heading corpora hash identically and do not move.
CHUNKER_VERSION = 3


def chunker_fingerprint(chunk_tokens, chunk_overlap, contextual_prefix) -> str:
    """The identity of the code+settings that built an index.

    ONE implementation, called by both `import` (which acts on a mismatch) and
    `doctor` (which reports one). `import` used to own the only copy, so `doctor`
    reported the embedding fingerprint, said nothing about the chunker, and gave
    a v2 index under v3 code a clean bill of health — the same
    looks-correct-on-a-fresh-install blind spot A-435 was written to close, one
    layer up (A-517).
    """
    return ":".join(str(x) for x in (
        CHUNKER_VERSION, chunk_tokens, chunk_overlap, int(bool(contextual_prefix)),
    ))

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|\-]+\|\s*$")


def _table_header_at(lines: list[str], i: int) -> list[str] | None:
    """The two header lines of a markdown table starting at line ``i``, else None."""
    if i + 1 < len(lines) and _TABLE_ROW.match(lines[i]) and _TABLE_SEP.match(lines[i + 1]):
        return [lines[i], lines[i + 1]]
    return None


def _window_section(doc: "Doc", sec: str, max_tokens: int, overlap: int,
                    ctx: str, idx: int, hpath: str = "") -> tuple[list["Chunk"], int]:
    """Split an oversized section without cutting a line, or orphaning table rows.

    Closes A-412. The previous path token-sliced the section, so a boundary could
    land mid-row and — far worse for a corpus whose house style mandates *tables
    over prose* — a continuation chunk could carry rows with **no header**. Rows
    without their header are not degraded, they are uninterpretable: `| 0.25 | ✅ |`
    means nothing without the columns naming it. Structured serving displays chunk
    text verbatim, so that lands in front of the model exactly as stored.

    Two guarantees:
      1. a line is never split (only a single line that alone exceeds the budget
         falls back to token slicing, which is unavoidable);
      2. when a break falls INSIDE a table, the next chunk is re-seeded with that
         table's header + separator, so every chunk of a table is self-describing.

    Non-table content keeps a trailing-line overlap, preserving the continuity the
    old token window provided. Sections that fit within ``max_tokens`` never reach
    this function, so the blast radius is oversized sections only.
    """
    lines = sec.split("\n")
    out: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    header: list[str] = []
    header_idx = -1

    # Only the FIRST window carries this section's own heading in its text; every
    # continuation starts mid-section and had no heading at all. Give those the
    # section heading as an ancestor, so a table row split off page 3 still says
    # what section it came from.
    own = lines[0].strip() if lines and HEADER_RE.match(lines[0]) else ""
    own_text = re.sub(r"^#{1,6}\s+", "", own) if own else ""
    cont_path = " > ".join(p for p in (hpath, own_text) if p)

    def _tok(text: str) -> int:
        return len(_encoder().encode(text))

    def _flush() -> list[str]:
        """Emit the buffer; return its trailing lines within the overlap budget."""
        nonlocal buf, buf_tokens, idx, out
        if not buf:
            return []
        path = hpath if not out else cont_path
        out.append(_make_chunk(doc, idx, "\n".join(buf), buf_tokens, ctx, path))
        idx += 1
        tail: list[str] = []
        total = 0
        for line in reversed(buf):
            t = _tok(line + "\n")
            if total + t > overlap:
                break
            tail.insert(0, line)
            total += t
        buf, buf_tokens = [], 0
        return tail

    i = 0
    while i < len(lines):
        line = lines[i]
        found = _table_header_at(lines, i)
        if found:
            header, header_idx = found, i
        elif not _TABLE_ROW.match(line):
            header, header_idx = [], -1

        lt = _tok(line + "\n")
        if lt > max_tokens:
            # One line larger than the whole budget. Nothing to preserve — slice it.
            _flush()
            toks = _encoder().encode(line)
            step = max(1, max_tokens - overlap)
            for start in range(0, len(toks), step):
                out.append(_make_chunk(
                    doc, idx, _encoder().decode(toks[start : start + max_tokens]),
                    min(max_tokens, len(toks) - start), ctx,
                    hpath if not out else cont_path))
                idx += 1
            i += 1
            continue

        if buf and buf_tokens + lt > max_tokens:
            tail = _flush()
            # Inside a table (and past its own header lines) → re-seed with the
            # header instead of the tail, so the rows that follow stay readable.
            if header and i > header_idx + 1:
                buf = list(header)
            else:
                buf = tail
            buf_tokens = _tok("\n".join(buf)) if buf else 0

        buf.append(line)
        buf_tokens += lt
        i += 1

    _flush()
    return out, idx


def _split_on_headers(body: str) -> list[tuple[str, str]]:
    """Split body on H1/H2 boundaries; each section keeps its header.

    Returns ``(section_text, heading_path)``. The path holds the ancestors ABOVE
    the section's own heading — for an H2 that is the H1 it lives under, which
    the split otherwise discards. That discard is A-513: a doc titled
    ``# RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25`` splits into H2
    sections, and the section holding a superseded count served as a live
    blocker because nothing in its chunk said the work was finished, or when.
    """
    matches = list(re.finditer(r"^(#{1,2})\s+(.+)$", body, re.MULTILINE))
    if not matches:
        return [(body, "")]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        sections.append((body[: matches[0].start()].rstrip(), ""))
    h1 = ""
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.start() : end].rstrip()
        if len(m.group(1)) == 1:
            # An H1 IS the root — its own heading is in `text`, so no ancestors.
            sections.append((text, ""))
            h1 = m.group(2).strip()
        else:
            sections.append((text, h1))
    return [(t, p) for t, p in sections if t.strip()]


def _make_chunk(doc: Doc, idx: int, text: str, tokens: int, context: str = "",
                heading_path: str = "") -> Chunk:
    # No context → legacy hash sha1(text) byte-for-byte, so flipping the flag
    # OFF leaves change-detection identical to pre-context builds. With context,
    # fold it in so flipping ON is correctly seen as a change.
    #
    # heading_path folds in on the same principle: it reaches the embedding and
    # the FTS row, so a chunk that gains ancestry is a CHANGED chunk and must be
    # re-embedded. A doc with no H2-under-H1 nesting has an empty path and keeps
    # its legacy hash byte-for-byte, so this does not churn flat corpora.
    payload = text
    if context:
        payload = f"{context}\x00{payload}"
    if heading_path:
        payload = f"{heading_path}\x01{payload}"
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return Chunk(
        doc_path=doc.rel_path, chunk_idx=idx, text=text, token_count=tokens,
        chunk_hash=h, context=context, heading_path=heading_path,
    )


def _first_header(body: str) -> str | None:
    m = HEADER_RE.search(body)
    return m.group(2).strip() if m else None
