# Workspace doctrine

The house rules every agent in this workspace inherits — inlined into each agent's envelope. Keep it
short; add rules only when a real run teaches you one. Two invariants are load-bearing and must not be
weakened:

## Isolation invariant

A run writes only inside its own run dir (`runs/<run-id>/`). It must never read from or write to a
sibling run, a shared cache, or anywhere outside its run dir. This is what makes runs resumable,
auditable, and safe to execute concurrently. Anything you need to hand to another run is an artifact;
anything you need to publish *outside* the run dir is an explicit final `run:` step in the pipeline
(visible in the plan, gated by validators) — never a quiet side write.

## Artifact-authority rule

Artifacts are the truth, not the conversation. A step is *done* only when the artifacts it `produces`
exist and pass their schema/validator — the STEP block an agent emits is a summary, and artifact
validation outranks it in both directions. State lives in files on disk, never in agent memory or
inter-agent chatter. If it isn't written to a declared artifact, it didn't happen.
