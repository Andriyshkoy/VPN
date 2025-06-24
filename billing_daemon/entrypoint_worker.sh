#!/bin/sh
exec python -m billing_daemon.rq_worker
