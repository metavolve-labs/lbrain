"""lbrain lair from-repo — convert a code repo + its README/CLAUDE.md into a
filled, governance-conformant LAIR.md.

Deterministic harvest (Stage 1) + Python-resolved Status/Priority (Stage 2, so the
model can't hallucinate them) + Gemini fill (Stage 3) + deterministic lint (Stage 4)
+ write (Stage 5) + re-sync (Stage 6). See docs/lair-framework/FAST_START_PROTOCOL.md.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
from pathlib import Path

import httpx

from .config import Config

STATUSES = ["PLANNING", "ACTIVE", "OPERATIONAL", "BLOCKED", "DORMANT"]
PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_TEMPLATE = """# {Lair Name}

**Status**: {PLANNING | ACTIVE | OPERATIONAL | BLOCKED | DORMANT}
**Priority**: {CRITICAL | HIGH | MEDIUM | LOW}
**Last Updated**: {YYYY-MM-DD}
**Mission**: {One sentence — what this lair exists to accomplish.}

> **Why blocked/paused**: {Only when BLOCKED/PAUSED — one line. Delete otherwise.}

---

## Current State

| Aspect | Status | Notes |
|--------|--------|-------|
| {Component} | {✅ Working / 🔄 In progress / ⬜ Not started / ❌ Broken} | {detail} |

**Blocked by**: {What blocks progress, or "Nothing".}
**Next action**: {The single most important next step.}

---

## Architecture

{ASCII diagram or 2-4 sentences. Omit if N/A.}

### Key Files

| File | Purpose |
|------|---------|
| `path/to/file.ext:line` | {reference, do not paste code} |

### Key Commands

```
{build / test / deploy command}   # what it does
```

---

## Decisions Log

- **{YYYY-MM-DD}**: {DECISION: X over Y because Z.}

---

## Implementation Checklist

- [ ] {Open task}

---

## Related Lairs

| Lair | Relationship |
|------|--------------|
| `{other-lair}/` | {Dependency / consumer / sibling} |

---

*{Optional one-line motto.}*
"""


def _run_git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _doc_title(docs: dict[str, str]) -> str:
    """The real document title = an H1 on the FIRST non-blank line of CLAUDE/README.
    A '# ...' that only appears mid-document is a section header, not the title."""
    for fn in ("CLAUDE.md", "README.md"):
        if fn not in docs:
            continue
        for line in docs[fn].splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("# "):
                t = re.sub(r"^CLAUDE\.md\s*[—-]\s*", "", s[2:]).strip()
                return t.lstrip("-—–•* \t").strip()
            break  # first content line isn't an H1 → no usable top title
    return ""


_ACRONYMS = {"Api": "API", "Sdk": "SDK", "Ai": "AI", "Mcp": "MCP", "Gcp": "GCP", "Nft": "NFT", "Ml": "ML"}


def _humanize(basename: str) -> str:
    t = basename.replace("-", " ").replace("_", " ").title()
    return " ".join(_ACRONYMS.get(w, w) for w in t.split())


def collect_repo_facts(repo: Path) -> dict:
    """Stage 1 — pure-Python deterministic signal harvest."""
    docs: dict[str, str] = {}
    for fn in ("README.md", "CLAUDE.md", "START_HERE.md"):
        p = repo / fn
        if p.exists():
            docs[fn] = p.read_text(encoding="utf-8", errors="replace")[:24000]
    blob = "\n".join(docs.values())

    artifacts = [a for a in (
        "Dockerfile", "firebase.json", "deploy.sh", "pyproject.toml",
        "package.json", "cloudbuild.yaml", "docker-compose.yml",
    ) if (repo / a).exists()]
    artifacts += [p.name for p in repo.glob("*.tf")]

    h1 = re.search(r"^#\s+(.+)$", blob, re.M)
    mission = (
        re.search(r"^>?\s*\*\*Mission\*\*[:：]?\s*(.+)$", blob, re.M)
        or re.search(r"^##\s*Mission\s*\n+>?\s*(.+)$", blob, re.M)
        or re.search(r"\*\*What this is\*\*[:：]?\s*(.+)", blob)
    )

    return {
        "repo_path": str(repo),
        "basename": repo.name,
        "last_commit_date": _run_git(repo, "log", "-1", "--format=%cs"),
        "commit": _run_git(repo, "log", "-1", "--format=%H")[:12],
        "remote": _run_git(repo, "remote", "get-url", "origin"),
        "docs": docs,
        "deploy_artifacts": artifacts,
        "h1": (h1.group(1).strip() if h1 else ""),
        "h1_top": _doc_title(docs),
        "mission_hint": (mission.group(1).strip() if mission else ""),
        "tables": re.findall(r"(?:^\|.*\|\s*$\n?){2,}", blob, re.M)[:12],
        "annotated": re.findall(r"^.*#\s+.{3,}$", blob, re.M)[:40],
        "file_refs": list(dict.fromkeys(re.findall(r"`?([\w./-]+\.\w+:\d+)`?", blob)))[:20],
        "bash_blocks": re.findall(r"```(?:bash|sh)\n(.*?)```", blob, re.S)[:6],
        "blockers": sorted({b for b in re.findall(
            r"(?i)\b(BLOCKED|needs deploy|broken|regression|TODO|WIP|min=0|COLD)\b", blob)}),
        "urls": list(dict.fromkeys(re.findall(r"https?://[\w.-]+[\w./?=&%-]*", blob)))[:20],
    }


def _age_days(date_str: str) -> int | None:
    try:
        d = datetime.date.fromisoformat(date_str)
        return (datetime.date.today() - d).days
    except Exception:
        return None


def infer_status(facts: dict) -> str:
    """Stage 2a — deterministic Status (kept out of the LLM's hands)."""
    blob = " ".join(facts["docs"].values()).lower()
    hard_block = {b.lower() for b in facts["blockers"]} & {
        "blocked", "broken", "regression", "needs deploy"}
    deployed = bool(facts["deploy_artifacts"]) and any(
        k in blob for k in ("deployed", "operational", "production", " live", "shipped"))
    if hard_block:
        return "BLOCKED"
    if deployed:
        return "OPERATIONAL"
    age = _age_days(facts["last_commit_date"])
    if not facts["docs"]:
        return "PLANNING"
    if age is not None and age > 60:
        return "DORMANT"
    return "ACTIVE"


def infer_priority(facts: dict, flag: str | None) -> str:
    """Stage 2b — deterministic Priority (flag overrides)."""
    if flag:
        return flag.upper()
    blob = " ".join(facts["docs"].values()).lower()
    path = facts["repo_path"].lower()
    if "_archive" in path:
        return "LOW"
    if "000-priority" in blob or "launch ready" in blob or "launch-ready" in blob:
        return "HIGH"
    if any(k in blob for k in ("x402", "stripe", "payment", "revenue", "live keys")):
        return "HIGH"
    if any(k in blob for k in ("research", "experiment", "paper", "benchmark")):
        return "MEDIUM"
    return "MEDIUM"


def _title(facts: dict, name: str | None) -> str:
    if name:
        return name
    top = facts.get("h1_top", "")
    if top and len(top.split()) <= 8:
        return top
    return _humanize(facts["basename"])


def folder_name(title: str, priority: str) -> str:
    if priority in ("CRITICAL", "HIGH"):
        up = re.sub(r"[^A-Z0-9]+", "-", title.upper()).strip("-")
        return f"000-PRIORITY-{up}"
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def build_prompt(facts: dict, fixed: dict, template: str) -> tuple[str, str]:
    system = (
        "You are a Lair Compiler. Convert a repository's documentation into ONE "
        "governance-conformant LAIR.md. Output ONLY the markdown document — no code "
        "fences around the whole thing, no preamble, no commentary.\n\n"
        "HARD RULES:\n"
        "- Follow the TEMPLATE skeleton exactly: section names, order, table headers.\n"
        "- First 5 lines: '# Title', then **Status**, **Priority**, **Last Updated**, "
        "**Mission**. Status/mission graspable in the first 30 lines.\n"
        "- Tables over prose. NEVER paste source code; reference files as path:line. "
        "Code blocks only for load-bearing commands.\n"
        "- One concern only (this repo). Other projects appear ONLY as Related Lairs rows.\n"
        "- <=500 lines. Long inventories: keep ~15 rows + '(see repo docs for full list)'.\n"
        "- Decisions Log: one line each, '- **YYYY-MM-DD**: DECISION: X over Y because Z'.\n"
        "- MUST end with a non-empty '## Related Lairs' table (| Lair | Relationship |). "
        "If none found, add a row for the repo's own remote/parent.\n"
        "- Do NOT invent facts. Use only the supplied RepoFacts. Leave optional sections "
        "out rather than padding.\n\n"
        f"FIXED FIELDS (copy verbatim, do not re-derive):\n"
        f"- Title: {fixed['title']}\n- Status: {fixed['status']}\n"
        f"- Priority: {fixed['priority']}\n- Last Updated: {fixed['last_updated']}\n\n"
        "FIELD GUIDE:\n"
        "- Mission: one sentence (prefer mission_hint).\n"
        "- Current State rows: one per subsystem/tool in the tables/annotated tree; "
        "normalize cell status to Working/Blocked/Broken. Then Blocked-by (from blockers "
        "or 'Nothing') and Next-action.\n"
        "- Architecture: copy any ASCII diagram; else synthesize from structure. Add a "
        "Key Commands subsection from the bash blocks (keep inline # comments).\n"
        "- Key Files: from annotated tree lines + file_refs (cap ~12).\n"
        "- Decisions Log: from 'Critical Patterns / DO NOT BREAK' or 'X over Y because Z'.\n"
        "- Related Lairs: from cross-workspace/sibling/parent lines + the remote.\n"
        "- If live URLs / contract addresses exist, append a trailing '## Live URLs' table.\n\n"
        "TEMPLATE (fill exactly):\n" + template
    )
    import json as _json
    facts_for_llm = {k: v for k, v in facts.items() if k != "docs"}
    facts_for_llm["doc_excerpts"] = {k: v[:6000] for k, v in facts["docs"].items()}
    user = (
        f"RepoFacts JSON for {facts['repo_path']}:\n\n"
        f"{_json.dumps(facts_for_llm, indent=2, ensure_ascii=False)}\n\n"
        "Produce the filled LAIR.md now."
    )
    return system, user


def generate_lair(cfg: Config, system: str, user: str, model: str) -> str:
    """Stage 3 — Gemini fill (LBrain is Gemini-native)."""
    key = cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_3_API_KEY", "")
    if not key:
        raise RuntimeError("No GEMINI_API_KEY configured (add to ~/.lbrain/env).")
    with httpx.Client(timeout=180.0) as c:
        r = c.post(
            f"{GEMINI_BASE}/models/{model}:generateContent",
            headers={"x-goog-api-key": key},  # header, not ?key= — keeps the key out of error/log URLs
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.3},
            },
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def lint(text: str) -> tuple[str, list[str]]:
    """Stage 4 — deterministic validate + light repair."""
    lines = text.splitlines()
    # strip accidental whole-doc code fences
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    text = "\n".join(lines)
    w: list[str] = []
    if not (lines and lines[0].startswith("# ")):
        w.append("missing H1 title on line 1")
    if not re.search(r"^\*\*Status\*\*:\s*(%s)" % "|".join(STATUSES), text, re.M):
        w.append("Status missing/invalid")
    if not re.search(r"^\*\*Priority\*\*:\s*(%s)" % "|".join(PRIORITIES), text, re.M):
        w.append("Priority missing/invalid")
    if not re.search(r"^\*\*Last Updated\*\*:\s*\d{4}-\d{2}-\d{2}", text, re.M):
        w.append("Last Updated missing/invalid date")
    if "## Current State" not in text:
        w.append("missing Current State section")
    if "## Related Lairs" not in text:
        w.append("missing Related Lairs section")
    n = len(text.splitlines())
    if n > 500:
        w.append(f"exceeds 500-line cap ({n} lines) — split required")
    elif n > 300:
        w.append(f"over 300 lines ({n}) — consider trimming")
    return text, w


def run_from_repo(repo_path, dest, name, priority, model, dry_run, no_embed, echo=print):
    repo = Path(repo_path).resolve()
    cfg = Config.load()

    facts = collect_repo_facts(repo)
    title = _title(facts, name)
    status = infer_status(facts)
    prio = infer_priority(facts, priority)
    last_updated = facts["last_commit_date"] or datetime.date.today().isoformat()
    folder = folder_name(title, prio)

    echo(f"  repo: {repo.name} | title: {title!r}")
    echo(f"  inferred → Status={status}  Priority={prio}  folder={folder}/")
    echo(f"  signals: {len(facts['docs'])} doc(s), {len(facts['deploy_artifacts'])} deploy artifact(s), "
         f"{len(facts['tables'])} table(s), blockers={facts['blockers'] or 'none'}")

    fixed = {"title": title, "status": status, "priority": prio, "last_updated": last_updated}
    system, user = build_prompt(facts, fixed, DEFAULT_TEMPLATE)
    echo(f"  filling with {model}…")
    doc = generate_lair(cfg, system, user, model)
    doc, warnings = lint(doc)
    if warnings:
        for x in warnings:
            echo(f"  ⚠️  {x}")
    else:
        echo("  ✅ lint clean")

    if dry_run:
        echo("\n" + "=" * 60 + " (dry-run, not written)\n")
        echo(doc)
        return

    dest_dir = Path(dest).expanduser() if dest else (cfg.sources[0] if cfg.sources else Path.cwd())
    out = Path(dest_dir) / folder / "LAIR.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc + "\n", encoding="utf-8")
    echo(f"\n  ✍️  wrote {out}")
    echo("  manual follow-ups (governance): add bidirectional rows in the named Related "
         "Lairs; add this lair to START_HERE/index.")

    if not no_embed:
        echo("  re-syncing index…")
        for cmd in (["lbrain", "import", str(dest_dir)], ["lbrain", "embed", "--stale"]):
            try:
                subprocess.run(cmd, timeout=300)
            except Exception as e:
                echo(f"  (re-sync step {cmd[1]} skipped: {e})")
    echo("  done.")
