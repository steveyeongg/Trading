"""Validation: the gate between a setup "hypothesis" and a Live setup (spec §7.8).

``protocol`` fixes the windows, the in-sample grid, the out-of-sample budget and the ship gate;
``prereg`` writes and checks the committed pre-registration; ``run`` runs the in-sample grid and
the one-shot out-of-sample test, writes the versioned reports under ``ml/validation/``, and reads
them back as the verdicts the nightly scan uses.
"""
