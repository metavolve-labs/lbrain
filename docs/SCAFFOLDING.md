# Scaffolding — running multiple agents on shared memory without them corrupting each other

LBrain's own development is a multi-agent operation: several AI seats, on different
machines, sharing corpora, channels, and repos. Everything below was learned by breaking
something first. It is development doctrine for anyone building a multi-agent system on
LBrain — the ecosystem is for builders who can do what they want; this is what we learned
paying full price, offered so you can pay less.

## 1. Compartmentalize first; widen deliberately

Start every agent with the narrowest view that lets it work (its own brain home, a scoped
manifest of sources, its own workspace outside any tree that carries another agent's
instructions). Widen when usefulness demands it — as an explicit, recorded grant, not
ambient drift. The strongest version we've seen in practice: an agent that *could* reach a
repo over an API and refused, because its scope said no and widening was the operator's
call. Scope discipline you can trust is what makes widening cheap later; the test of
compartmentalization is what earns the expansion.

## 2. One writer per surface

One agent per repo, per register file, per channel entry. Two agents editing one document
will each behave correctly and still lose data. Where two must share, share a *directory*
of small files, never a file: a POSIX rename is atomic, so claiming work is `mv` — the
loser gets an error instead of a silent duplicate.

## 3. Constraints travel with the delegation

A publish gate, a scope limit, a "don't ship before X" — if it isn't written INSIDE the
message that hands over the work, it does not exist on the other machine. We learned this
by having a do-not-publish note sit as an untracked local file while the work it governed
was routed to another agent; the mechanism was published within days. The rule that came
out of it: every delegation message carries its own gates, inline.

## 4. Configured ≠ delivered ≠ processed

"I routed the request" is not "they received it," is not "they did it," is not "it's
correct." Each is a separate claim needing its own evidence. The most expensive phantom is
the deliverable everyone believes exists because everyone heard someone else mention it —
before building on a verdict, artifact, or review, confirm the artifact itself exists and
name where. A scope-limited negative ("not in the places I can reach, which are…") beats a
confident absolute in both directions.

## 5. Channel hygiene

One file per message, UTC timestamps with the `Z` in the filename, explicit `to:`/`from:`
(a role name is not a session identity — two sessions can both be "the reviewer").
Prepending entries to one shared log file will eventually bury a report below newer
noise and someone will truthfully say "nothing was posted." An index file plus one file
per entry does not have that failure mode.

## 6. Corrections live inside the claim; numbers carry their derivations

A correction placed *beside* a wrong claim loses to the claim — retrieval serves the
chunk, not the errata. Edit the claim itself; keep the history in version control, not in
adjacent prose. And a derived number states its derivation inline (`45 min → 60 sec
(45×)`) so the next reader can check it in place — LLM-written corpora generate fluent,
authoritative, unchecked arithmetic, and nothing downstream re-derives it.

## 7. The brain serves what you feed it

Anything indexed is served to whatever queries the brain — so secrets are *pointed at*,
never pasted; imports carry a scan gate; and guards get self-tests, because a guard that
has never been asked to demonstrate a failure may be watching a fraction of its doorway
while reporting success.

## 8. Isolation is a property, not a mechanism

Test suites, experiment harnesses, and scratch agents run against homes with *nothing in
them to destroy* — not against the live install with patches promising to behave.
A mechanism can regress; "there is nothing here to lose" cannot.

---

None of this requires LBrain — it's how we run LBrain on itself. If you're wiring agents
to a shared brain, start with `lbrain setup` (per-agent, one dial-in each), give each its
own `LBRAIN_HOME`, and sync through source files in version control: the files are
authoritative, every index is derivative, and any box can rebuild from source.
