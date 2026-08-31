# Fusion design notes

_Fill this in during weeks 5–9. This is the document an interviewer is most
likely to ask about — write it as you go, not retroactively._

## State estimation

- Filter chosen (Kalman / alpha-beta / other) and why.
- What state is tracked per entity (position, velocity, heading, ...).

## Gating

- How candidate pairs across sources are narrowed down before association
  runs (distance window, time window, etc.).

## Association

- Algorithm used (nearest-neighbor, Hungarian/assignment problem, other).
- Alternatives considered and why they were rejected — this matters more
  than the final choice.

## Conflict resolution

- What happens when the two sources disagree on position/velocity for what
  looks like the same track.
- What happens when a source drops a track and it reappears later, possibly
  with a new ID.

## Known limitations

- Be specific and honest here. "Doesn't handle X because Y, would need Z to
  fix" is a stronger interview answer than pretending there are no gaps.
