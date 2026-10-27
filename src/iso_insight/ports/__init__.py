"""Ports — the interfaces the domain uses to talk to the outside world.

A port may import only ``schemas``, stdlib and pydantic (enforced by
import-linter). Implementations live in ``adapters/`` and are chosen in
the composition root (``bootstrap.py``).
"""
