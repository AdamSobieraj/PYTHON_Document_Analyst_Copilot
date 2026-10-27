"""Composition root — the only place where concrete adapters are chosen.

``settings -> concrete adapters -> context`` (CLAUDE.md): which adapter
implements a port is a configuration decision (env var), made here and
nowhere else. Delivery layers (``api``, ``workers``) build the context
through this module and call pipeline stages — they hold no business
logic and never construct clients themselves.

Phase 1 ships the target layout's skeleton; the actual wiring arrives
with the adapters (later phases). Until then this module is a stub so
the import contracts have a real target.
"""
