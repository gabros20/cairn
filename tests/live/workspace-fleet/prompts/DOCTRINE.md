# Workspace doctrine

House rules every agent in this mixed-fleet workspace inherits.

## Artifact-authority rule

Artifacts are the truth, not the conversation. A step is *done* only when the artifacts it
`produces` exist and pass their schema/validator. The STEP block you emit is a summary;
artifact validation outranks it. If it isn't written to a declared artifact, it didn't happen.

## Keep it tight

This is a plumbing proof, not a reasoning test. Do exactly what the CONTRACT asks — read the
listed inputs, write the requested artifact with exactly the requested keys, emit the STEP
block, and stop. No extra files, no exploration. When an input artifact is listed, read it and
copy values from it verbatim; never guess them.
