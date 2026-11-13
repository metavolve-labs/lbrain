"""Candidate visible-turn normalization for Gemini CLI transcripts (pure function).

Increment for LBrain Archiver: turns a Gemini CLI session transcript (JSONL
records with producer metadata, MongoDB-style delta operations, and user/gemini
turns) into clean, human-readable markdown dialogue turns for snapshotting and
semantic archival. Returns None when the text is not a Gemini transcript.
"""
from __future__ import annotations

import json
from typing import Any


def render_gemini_transcript_jsonl(text: str, *, cap: int = 1_000_000) -> str | None:
    """Render a Gemini CLI transcript JSONL to readable markdown dialogue turns.

    What is kept:
      - Visible user prompts (excluding harness setup context blocks).
      - Visible model response text (final synthesis, answers, user explanations).
      - Honest timestamp labels per turn.

    What is dropped:
      - Producer session metadata rows (kind, projectHash, startTime).
      - In-flight MongoDB-style state mutations ($set, $patch).
      - Harness status announcements (type: info).
      - Model internal thoughts/reasoning chains (thoughts block).
      - Tool execution requests (toolCalls / functionCall payloads).
      - Harness context setup injections (<session_context>, <loaded_context>).

    Returns None if the payload is not a valid Gemini CLI session transcript,
    allowing fallback to other format handlers or extractive processing.
    """
    if not text or not text.strip():
        return None

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)

    if not rows:
        return None

    # Require explicit Gemini session header on the first row
    r0 = rows[0]
    if not (
        isinstance(r0, dict)
        and "sessionId" in r0
        and ("projectHash" in r0 or "startTime" in r0 or "kind" in r0)
    ):
        return None

    # Ensure at least one substantive dialogue turn exists
    if not any(r.get("type") in ("user", "gemini") for r in rows):
        return None

    out: list[str] = []
    used = 0

    for row in rows:
        kind = row.get("type")
        piece = ""
        raw_ts = str(row.get("timestamp") or "")
        ts = raw_ts[11:19] if len(raw_ts) >= 19 else raw_ts

        if kind == "user":
            content = row.get("content")
            visible: list[str] = []
            if isinstance(content, str):
                v = content.strip()
                if not v.startswith(("<session_context>", "<loaded_context>")):
                    visible.append(v)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        v = item["text"].strip()
                        if v.startswith(("<session_context>", "<loaded_context>")):
                            continue
                        if v:
                            visible.append(v)
                    elif isinstance(item, str):
                        v = item.strip()
                        if not v.startswith(("<session_context>", "<loaded_context>")):
                            visible.append(v)

            if visible:
                header = f"## user {ts}".strip()
                piece = f"{header}\n" + "\n".join(visible)

        elif kind == "gemini":
            content = row.get("content")
            visible = []
            if isinstance(content, str):
                v = content.strip()
                if v:
                    visible.append(v)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        v = item["text"].strip()
                        if v:
                            visible.append(v)
                    elif isinstance(item, str):
                        v = item.strip()
                        if v:
                            visible.append(v)

            if visible:
                header = f"## gemini {ts}".strip()
                piece = f"{header}\n" + "\n".join(visible)

        if not piece:
            continue

        if used + len(piece) > cap:
            out.append("… [render truncated at cap — full session in the archived record]")
            break

        out.append(piece)
        used += len(piece) + 2

    return "\n\n".join(out) if out else None
