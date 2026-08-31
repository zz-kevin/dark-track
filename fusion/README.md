# fusion/

The technical core of the project (weeks 5–9). Turns two noisy,
uncorrelated track streams into one confident picture per real-world entity.

- Per-track state estimation (Kalman or alpha-beta filter)
- Gating — which track pairs are even candidates for the same object
- Association — nearest-neighbor or the Hungarian algorithm, resolved each
  tick
- Conflict resolution — source disagreement, dropout, re-appearance under a
  new ID
- Confidence scoring on the emitted fused track

Design reasoning and rejected alternatives go in
[`../docs/FUSION.md`](../docs/FUSION.md), not just in code comments.
