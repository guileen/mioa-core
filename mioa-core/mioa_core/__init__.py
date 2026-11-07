"""Shared Mioa native runtime core.

Owns native launch/config/profile identity, page references, ownership leases,
the declared operation table and the headless engine used by `mioa_headless`.
It imports no ATB CLI, desktop UI, Camoufox/Playwright adapter or HTTP viewer.
"""

__version__ = "0.1.0"
