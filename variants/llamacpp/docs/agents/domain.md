# Domain docs

This is a single-context repository.

## Before exploring

- Read root `CONTEXT.md` if it exists.
- Read relevant decisions in `docs/adr/` if that directory exists.
- If these files are absent, proceed silently. Do not flag their absence or suggest creating them upfront; domain docs and ADRs are added when concepts or decisions are resolved.

## Use the glossary

Use domain terms as defined in `CONTEXT.md` in issues, proposals, hypotheses, and tests. If a needed concept is missing, reconsider whether it is project language or note the gap for domain modeling; do not invent a competing synonym.

## Surface ADR conflicts

If proposed work contradicts an existing ADR, state the conflict explicitly rather than silently overriding it (for example: “Contradicts ADR-0007, but worth reopening because…”).
