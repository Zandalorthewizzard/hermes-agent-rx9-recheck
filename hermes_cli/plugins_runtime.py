"""Host-owned runtime slots for profile-scoped native plugins.

A runtime slot is deliberately separate from the imported plugin module. Directory plugin modules are
replaced on force discovery, while the owning :class:`PluginManager` remains alive for the profile.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


_MISSING = object()


class PluginRuntimeSlot:
    """One manager-owned, profile/plugin/local-key runtime value.

    Replacement discovery leaves the slot intact. Definitive manager teardown calls the registered
    cleanup once and then discards the value. The callback is intentionally zero-argument: plugins
    close the resource they acquired through a closure, matching ``PluginContext.on_unload``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._value: Any = _MISSING
        self._cleanup: Callable[[], None] | None = None
        self._cleaned = False

    def get_or_create(self, factory: Callable[[], Any]) -> Any:
        """Return the cached value, or build it once under the slot lock.

        A failed factory is never cached: the slot remains absent and a later call may retry.
        """
        if not callable(factory):
            raise TypeError("runtime slot factory must be callable")
        with self._lock:
            if self._cleaned:
                raise RuntimeError("runtime slot is definitively closed")
            if self._value is not _MISSING:
                return self._value
            value = factory()
            self._value = value
            return value

    def clear_on_unload(self, *, cleanup: Callable[[], None]) -> None:
        """Set the definitive-lifecycle cleanup callback for this slot."""
        if not callable(cleanup):
            raise TypeError("runtime slot cleanup must be callable")
        with self._lock:
            if self._cleaned:
                return
            self._cleanup = cleanup

    def _cleanup_definitive(self) -> None:
        """Run cleanup at most once; manager teardown owns the lifecycle boundary."""
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            cleanup = self._cleanup
            self._cleanup = None
            self._value = _MISSING
        if cleanup is not None:
            cleanup()
