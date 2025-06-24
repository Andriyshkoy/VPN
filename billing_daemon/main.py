"""Deprecated entrypoint for the billing daemon.

The previous version spawned worker and scheduler processes via
``multiprocessing`` which caused issues inside Docker. Use the dedicated
``rq_worker`` and ``rq_scheduler`` entrypoints instead.
"""
