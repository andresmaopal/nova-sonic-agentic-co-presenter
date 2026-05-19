"""SlideStore — in-memory store for preprocessed slides and vision analysis cache.

The store holds an immutable tuple of :class:`SlideData` along with a current-slide
pointer (updated by the keyboard-hook thread via ``set_current_index``) and a
cache of Nova Pro vision responses keyed by ``(slide_index, query)``.

Thread safety
-------------
Two producer threads touch this object:

* the **keyboard-hook thread** calls :meth:`set_current_index` whenever the
  presenter advances a slide;
* the **audio / tool thread** calls :meth:`get_current_slide`,
  :meth:`get_cached_analysis`, and :meth:`cache` while servicing an
  ``analyze_slide`` tool call.

Every public method serialises on a single :class:`threading.RLock`, which is
cheap for this workload (no long-running work is held under the lock) and
gives a clean read-your-writes guarantee across threads.
"""

from __future__ import annotations

from threading import RLock
from typing import Iterable, Optional, Tuple, Union

from src.models import SlideData

# A cache key is always normalised to ``(slide_index, query)``.
CacheKey = Tuple[int, str]


class SlideStore:
    """Holds loaded slides, current slide pointer, and a vision-analysis cache."""

    def __init__(self) -> None:
        self.slides: tuple[SlideData, ...] = ()
        self.current_index: int = 0
        self._cache: dict[CacheKey, str] = {}
        self._lock: RLock = RLock()

    # ------------------------------------------------------------------ #
    # Slide management
    # ------------------------------------------------------------------ #

    def load_slides(self, slide_data_list: Iterable[SlideData]) -> int:
        """Replace the loaded deck with ``slide_data_list`` and reset state.

        Args:
            slide_data_list: An iterable of :class:`SlideData` objects. The
                iterable is materialised into an immutable tuple snapshot.

        Returns:
            The number of slides loaded.

        Raises:
            TypeError: If any element is not a :class:`SlideData`.
            ValueError: If the iterable is empty (requirement 7.1 requires at
                least one valid slide to invoke the agent).
        """
        snapshot = tuple(slide_data_list)

        for item in snapshot:
            if not isinstance(item, SlideData):
                raise TypeError(
                    "SlideStore.load_slides expected SlideData, "
                    f"got {type(item).__name__}"
                )

        if not snapshot:
            raise ValueError(
                "SlideStore.load_slides requires at least one slide"
            )

        with self._lock:
            self.slides = snapshot
            self.current_index = 0
            self._cache.clear()
            return len(self.slides)

    def get_current_slide(self) -> SlideData:
        """Return the :class:`SlideData` at the current index.

        Raises:
            RuntimeError: If called before any slides have been loaded.
        """
        with self._lock:
            if not self.slides:
                raise RuntimeError(
                    "SlideStore.get_current_slide called before load_slides"
                )
            return self.slides[self.current_index]

    def set_current_index(self, index: int) -> None:
        """Update the current slide pointer after validating bounds.

        The new value is only assigned once all validation passes — this
        preserves the invariant ``0 <= current_index < total_slides`` required
        by Requirement 2.4 (an invalid call must not mutate state).

        Args:
            index: New 0-based slide index.

        Raises:
            ValueError: If ``index`` is not an ``int`` (booleans are rejected).
            IndexError: If ``index`` is out of ``[0, total_slides)``.
        """
        # bool is a subclass of int — reject it explicitly.
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(
                "SlideStore.set_current_index requires int, "
                f"got {type(index).__name__}"
            )

        with self._lock:
            total = len(self.slides)
            if index < 0 or index >= total:
                raise IndexError(
                    f"SlideStore.set_current_index index {index} "
                    f"out of bounds [0, {total})"
                )
            self.current_index = index

    @property
    def total_slides(self) -> int:
        """Number of slides currently loaded."""
        with self._lock:
            return len(self.slides)

    # ------------------------------------------------------------------ #
    # Vision analysis cache
    # ------------------------------------------------------------------ #

    def get_cached_analysis(self, index: int, query: str) -> Optional[str]:
        """Return the cached analysis for ``(index, query)`` or ``None``.

        A cache miss is a normal occurrence (first time a query is asked for
        a slide); callers should treat ``None`` as "go call the vision model".
        """
        with self._lock:
            return self._cache.get((index, query))

    def cache(
        self,
        key: Union[CacheKey, str],
        value: str,
    ) -> None:
        """Store a vision analysis result under ``key``.

        ``key`` may be provided in either of two equivalent forms to match
        both the design's pseudocode (``STRING(index) + ":" + query``) and a
        caller-friendly 2-tuple form:

        * ``(index, query)`` — an ``(int, str)`` tuple, stored directly.
        * ``"<index>:<query>"`` — a string; split on the first ``":"``, the
          prefix is parsed as an ``int`` and the remainder is used as the
          query verbatim.

        Raises:
            ValueError: If the string form is malformed (missing ``":"`` or
                a non-integer prefix), or if ``value`` is not a string.
            TypeError: If ``key`` is neither a 2-tuple nor a string.
        """
        if not isinstance(value, str):
            raise ValueError(
                "SlideStore.cache value must be str, "
                f"got {type(value).__name__}"
            )

        parsed_key = self._parse_cache_key(key)

        with self._lock:
            self._cache[parsed_key] = value

    @staticmethod
    def _parse_cache_key(key: Union[CacheKey, str]) -> CacheKey:
        """Normalise a cache key to a ``(int, str)`` tuple."""
        if isinstance(key, tuple):
            if len(key) != 2:
                raise ValueError(
                    "SlideStore.cache tuple key must be (index, query), "
                    f"got tuple of length {len(key)}"
                )
            index, query = key
            if not isinstance(index, int) or isinstance(index, bool):
                raise ValueError(
                    "SlideStore.cache key index must be int, "
                    f"got {type(index).__name__}"
                )
            if not isinstance(query, str):
                raise ValueError(
                    "SlideStore.cache key query must be str, "
                    f"got {type(query).__name__}"
                )
            return (index, query)

        if isinstance(key, str):
            if ":" not in key:
                raise ValueError(
                    "SlideStore.cache string key must be '<index>:<query>', "
                    f"got {key!r}"
                )
            prefix, _, query = key.partition(":")
            try:
                index = int(prefix)
            except ValueError as exc:
                raise ValueError(
                    "SlideStore.cache string key has non-integer index "
                    f"prefix: {prefix!r}"
                ) from exc
            return (index, query)

        raise TypeError(
            "SlideStore.cache key must be (int, str) tuple or str, "
            f"got {type(key).__name__}"
        )
