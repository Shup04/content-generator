"""Bounded stage concurrency; drain in-flight work before reporting a failure."""

from concurrent.futures import ThreadPoolExecutor, as_completed


def parallel_each(function, items, workers=1):
    if not 1 <= workers <= 4:
        raise ValueError("Parallel saves must be between 1 and 4")
    if workers == 1:
        for item in items:
            function(item)
        return
    errors = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="save-reel") as pool:
        futures = [pool.submit(function, item) for item in items]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)
    if errors:
        raise errors[0]
