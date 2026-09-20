# airspace

Corridor reservation, strategic conflict detection, closest point of approach,
and deconfliction resolution.

Resolution must be deterministic — the same conflict evaluated twice produces an
identical answer, so that two pilots looking at two consoles are told compatible
things. See `docs/ARCHITECTURE.md` §7.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.
Deconfliction changes are validated by scenarios in `sim/scenarios/`, not by
unit tests alone.
