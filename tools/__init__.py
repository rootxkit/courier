"""Operator-facing diagnostics.

Not part of the deployed system and deliberately not in the installed
distribution: these are run by hand, at a ground station, to produce evidence
for a decision record. They are held to the same lint and typing standard as
the services, with one documented exception — they print to stdout, because
their stdout is the deliverable.
"""
