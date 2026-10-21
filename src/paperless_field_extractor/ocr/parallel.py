"""Ordered parallel map for work that waits on external processes.

Threads are the right tool here: the work is subprocess calls (Tesseract, poppler)
and downloads, which release the GIL, and every task is independent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor


def parallel_map[T, R](function: Callable[[T], R], items: Sequence[T], workers: int) -> list[R]:
    """``[function(item) for item in items]`` on up to *workers* threads, in input order.

    Runs inline when there is nothing to parallelise. If a task raises, tasks that
    have not started are cancelled and the first error in input order is re-raised.
    """
    if workers <= 1 or len(items) <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        futures = [executor.submit(function, item) for item in items]
        try:
            return [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise
