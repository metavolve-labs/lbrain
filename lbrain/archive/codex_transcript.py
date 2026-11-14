"""Candidate visible-turn normalization for Codex rollouts (pure function)."""
import json


def render_codex_transcript_jsonl(text: str, *, cap: int = 1_000_000) -> str | None:
    rows=[]
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row=json.loads(line)
        except (ValueError,TypeError):
            continue
        if isinstance(row,dict):
            rows.append(row)
    # Require the producer's explicit session header, not a guessed filename or
    # arbitrary JSON containing role/message keys. Never treat plain text as JSONL.
    if not rows or rows[0].get('type') != 'session_meta' or not isinstance(rows[0].get('payload'),dict) or not rows[0]['payload'].get('id'):
        return None
    if not any(row.get('type')=='response_item' for row in rows):
        return None
    out=[]; used=0
    for row in rows:
        kind=row.get('type');piece=''
        if kind=='compacted':
            piece='## compact_boundary '+str(row.get('timestamp') or '')[:19]
        elif kind=='response_item':
            message=row.get('payload')
            if not isinstance(message,dict) or message.get('type')!='message' or message.get('role') not in ('user','assistant'):
                continue
            blocks=message.get('content')
            if not isinstance(blocks,list):
                continue
            visible=[]
            for block in blocks:
                if not isinstance(block,dict) or block.get('type') not in ('input_text','output_text','text') or not isinstance(block.get('text'),str):
                    continue
                value=block['text'].strip()
                # Harness-injected project/environment blocks arrive with role
                # user too. They are setup, not a user's conversational turn.
                if value.startswith(('# AGENTS.md instructions for ', '<environment_context>')):
                    continue
                if value:
                    visible.append(value)
            if visible:
                piece='## '+message['role']+' '+str(row.get('timestamp') or '')[11:19]+'\n'+'\n'.join(visible)
        if not piece:
            continue
        if used+len(piece)>cap:
            out.append('… [render truncated at cap — full session in the archived record]');break
        out.append(piece);used+=len(piece)+2
    return '\n\n'.join(out) if out else None
