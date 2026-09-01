"""Write-path gates W1/W2 — done-means-dereferenced at the engine chokepoint.

Design: CSO spec 2026-09-01T20:00Z (``_COLLAB`` inbox, "done-means-dereferenced
write-path gates"). The principle, stated once: a write target is a REFERENCE
(env var, config key, seat claim); nothing is "done" until the reference is
DEREFERENCED and the dereferenced object is checked against what the caller
claimed. Every defect in this family — capture-to-org-brain, the 2026-09-01
CSO ``db_path`` incident (a copied config's absolute path made the engine write
a probe into the LIVE brain while the caller named a scratch home), the A-546
impostor seed — is a reference honoured without a dereference check.

The gates live here, at ``open_store(for_write=True)``, because the engine is
the one chokepoint every harness shares (Claude, Grok, Codex, Chamber, and
harnesses that don't exist yet). Hooks are per-harness; this is not.

W1 — home coherence: the resolved effective db must live INSIDE the home the
caller named. No identity infrastructure needed; would have stopped the CSO
incident cold.

W2 — seat identity (hazard-13 rule 3 at brain grain): a caller carrying a seat
claim (``LBRAIN_SEAT``) must match the home's ``identity.json`` name,
component-wise — not substring (unanchored matching is how A-546 happened).

Cross-cutting rules (all from measured failures):
- Env tests are PRESENCE-aware: ``LBRAIN_SEAT`` set-but-empty is what a broken
  launcher expansion produces (RED-V2-1's shape) and REFUSES — it never
  silently selects the no-claim branch. The decision is right here in words.
- Refusal logging is UNCONDITIONAL and lands OUTSIDE any brain home
  (``~/.lbrain-refusals.log``), before the exception, never gated on any
  happy-path discovery.
- The CLI fails the command on refusal (rc=2, wired in cli.py); hook wrappers
  keep their own fail-safe exit 0 — by then the refusal line is on disk.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class WriteGateError(Exception):
    """A write was refused by W1 (home coherence) or W2 (seat identity)."""


def _log_refusal(message: str) -> None:
    """One line, outside any brain home, best-effort, never raises."""
    try:
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} REFUSED write: {message}\n"
        with open(Path.home() / ".lbrain-refusals.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _refuse(message: str) -> None:
    _log_refusal(message)
    raise WriteGateError(message)


def check_write_target(cfg, named_home: Path) -> None:
    """Run W1 then W2 for a write open. Raises WriteGateError on refusal.

    ``named_home`` is the home the caller named (CONFIG_DIR: the LBRAIN_HOME
    env when set, else ``~/.lbrain`` — W1 runs either way, guarding a config
    that points OUT of the default home too).
    """
    # --- W1: home coherence -------------------------------------------------
    # realpath BOTH sides: symlinks and ~ must not defeat containment (R2).
    effective_db = Path(cfg.db_path).expanduser().resolve()
    home_real = Path(named_home).expanduser().resolve()
    if not effective_db.is_relative_to(home_real):
        _refuse(
            f"named home {home_real} but config resolves db to {effective_db} — "
            "the home you name is not the home you would write. Fix db_path in "
            f"{home_real / 'config.toml'} (a copied config carrying the original's "
            "absolute db_path is the known shape of this defect)."
        )

    # --- W2: seat identity --------------------------------------------------
    # PRESENCE-aware: distinguish unset (no claim → W2 vacuous) from
    # set-but-empty (broken launcher expansion → refuse; never the quiet branch).
    if "LBRAIN_SEAT" not in os.environ:
        return
    claim = os.environ["LBRAIN_SEAT"].strip()
    if not claim:
        _refuse(
            "LBRAIN_SEAT is set but EMPTY — a broken launcher expansion, not the "
            "absence of a claim. Unset it for claimless writes, or export the seat."
        )

    id_path = Path(named_home) / "identity.json"
    if not id_path.is_file():
        _refuse(
            f"expected seat '{claim}' but home {home_real} carries no identity.json "
            "to check — provision the identity or drop the claim."
        )
    try:
        home_seat = json.loads(id_path.read_text(encoding="utf-8")).get("name", "")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A parse failure and a foreign seat are DIFFERENT diagnoses — name the
        # parse failure (CSO V4 note: the misleading-message class).
        _refuse(f"identity.json at {id_path} is unreadable/unparseable ({exc}) — cannot verify seat claim '{claim}'.")

    # Component-wise, not substring: 'cso' must match a path segment of
    # 'metavolvelabs/csuite/cso/touchstone', never merely appear inside one.
    # Multi-segment claims ('cso/touchstone') must match as a contiguous run.
    name_parts = [p for p in str(home_seat).split("/") if p]
    claim_parts = [p for p in claim.split("/") if p]
    n, c = len(name_parts), len(claim_parts)
    contiguous = any(name_parts[i : i + c] == claim_parts for i in range(n - c + 1)) if 0 < c <= n else False
    if not contiguous:
        _refuse(
            f"seat claim '{claim}' does not match home identity '{home_seat}' "
            f"(component-wise) — this home belongs to another seat."
        )
