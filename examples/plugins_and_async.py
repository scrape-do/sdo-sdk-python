# SPDX-License-Identifier: Apache-2.0
"""Generic plugin calls + the async job REST API (q.scrape.do/api/v1)."""

from sdo import Client

with Client() as client:  # reads SDO_TOKEN
    # Generic plugin core — any documented plugin, no SDK release needed:
    shopping = client.plugin("google/shopping", {"q": "laptop", "gl": "us"})
    print("plugin result type:", type(shopping).__name__)

    # Async job API: submit target URLs, poll status, fetch each task's result.
    # Auth is the X-Token header (handled by the client); methods return typed objects.
    job = client.jobs.create(["https://example.com"], {"Super": True, "GeoCode": "us"})
    print("submitted job:", job.job_id, "tasks:", job.task_ids)

    status = client.jobs.status(job.job_id)
    print("status:", status.status)
    for task in status.tasks:
        result = client.jobs.result(job.job_id, task.task_id)
        print(task.url, "->", result.status_code)

    # Account snapshot (concurrency + remaining credits):
    print("available credits:", client.jobs.me().available_credits)
