# dispatch

Assignment engine: eligibility filters, energy budget, scoring, batch
assignment on a 5 s tick.

The energy budget (`docs/ARCHITECTURE.md` §6) including the 35% reserve is the
single most important guard in the system. It is never relaxed to make an
assignment succeed.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.
