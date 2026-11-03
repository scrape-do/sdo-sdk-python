# SPDX-License-Identifier: Apache-2.0
"""sdo — the official Python SDK for the Scrape.do web-scraping API.

Quickstart::

    from sdo import Client, ScrapeConfig

    with Client(token="YOUR_TOKEN") as client:        # or set SDO_TOKEN
        result = client.scrape("https://example.com", render=True)
        print(result.status_code, len(result.bytes()))
        print(result.text[:200])
"""

from __future__ import annotations

from ._version import __version__
from .client import Client
from .config import ScrapeConfig
from .errors import (
    SdoAPIError,
    SdoAuthError,
    SdoConcurrencyError,
    SdoConfigError,
    SdoConnectionError,
    SdoError,
    SdoGatewayError,
    SdoResponseTooLargeError,
    SdoTimeoutError,
    SdoTransportError,
    SdoValidationError,
)
from .jobs import (
    JOB_SORTS,
    JOB_STATUSES,
    MAX_PLUGIN_PARAMS,
    Account,
    AsyncJobs,
    Job,
    JobPage,
    JobStatus,
    Task,
    TaskResult,
)
from .response import ScrapeResult
from .usage import Usage

__all__ = [
    "__version__",
    "Client",
    "ScrapeConfig",
    "ScrapeResult",
    "Usage",
    "AsyncJobs",
    "Job",
    "JobPage",
    "JobStatus",
    "JOB_SORTS",
    "JOB_STATUSES",
    "MAX_PLUGIN_PARAMS",
    "Task",
    "TaskResult",
    "Account",
    "SdoError",
    "SdoConfigError",
    "SdoTransportError",
    "SdoConnectionError",
    "SdoTimeoutError",
    "SdoResponseTooLargeError",
    "SdoAPIError",
    "SdoAuthError",
    "SdoValidationError",
    "SdoConcurrencyError",
    "SdoGatewayError",
]
