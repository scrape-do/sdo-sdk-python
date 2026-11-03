# SPDX-License-Identifier: Apache-2.0
"""Async job API (``q.scrape.do``) — the REST surface for background scrapes.

Auth is the client token, sent as the ``X-Token`` request header (never in the
URL). Requests go through the same transport as ``Client.scrape``: the same
size cap, total timeout, redaction, retry policy and error taxonomy, and a
redirect is an error, never followed. Methods return typed dataclasses.

The job body is a distinct PascalCase JSON schema — it is NOT the query-string
serialization used by :class:`~sdo.config.ScrapeConfig`. ``create(...)`` merges
your ``options`` map into the JSON body, using the API's own job-body keys
(``Super``, ``GeoCode``, ``Render``, ``Output``, …), and sends it as canonical
JSON.
"""

from __future__ import annotations

import base64
import binascii
import builtins
import datetime as _dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import _json
from ._secret import Secret
from ._transport import ASYNC, HEADER, Transport
from ._wire import jdump
from .errors import SdoConfigError, SdoTransportError

#: Job and task ids are validated before they enter the URL path, so an id from
#: untrusted input cannot reach another endpoint ("../me", "x?y", "x#").
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")

#: The job statuses the API documents (also the ``status`` filter of ``list``).
JOB_STATUSES = ("queuing", "queued", "pending", "rotating", "success", "error", "canceled")
#: The sort orders ``list`` accepts; ``start_time_desc`` (newest first) is the default.
JOB_SORTS = ("start_time_desc", "start_time_asc")
#: The documented limit on params entries (tasks) in one plugin job.
MAX_PLUGIN_PARAMS = 1000
#: A documented plugin key such as ``amazon/pdp`` or ``google/shopping/product/stores``.
_PLUGIN_KEY_RE = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)+\Z")
#: RFC 3339 with uppercase ``T`` and ``Z`` (the API is case-sensitive about both).
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})\Z")


@dataclass(kw_only=True)
class Job:
    """The acknowledgement returned by :meth:`AsyncJobs.create`."""

    job_id: str | None = None
    message: str | None = None
    task_ids: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class Task:
    """A single target within a job, as reported by :meth:`AsyncJobs.status`."""

    task_id: str | None = None
    url: str | None = None
    status: str | None = None


@dataclass(kw_only=True)
class JobStatus:
    """A job's lifecycle state and its per-target tasks."""

    job_id: str | None = None
    task_ids: list[str] = field(default_factory=list)
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    #: The job's share of the account's separate async concurrency pool.
    acquired_concurrency: int | None = None
    limit_concurrency: int | None = None
    canceled: bool = False
    tasks: list[Task] = field(default_factory=list)


@dataclass(kw_only=True)
class JobPage:
    """One page of :meth:`AsyncJobs.list`. The counts cover every job matching
    the filters; ``page_size`` and ``page_number`` echo the page served."""

    jobs: list[JobStatus] = field(default_factory=list)
    total_count: int | None = None
    page_size: int | None = None
    page_number: int | None = None
    total_pages: int | None = None


@dataclass(kw_only=True)
class TaskResult:
    """The scraped result for one task within a job.

    ``content`` is the body as the API returned it: base64 text when
    ``base64_encoded_content`` is true (binary targets), else the page text.
    :meth:`content_bytes` gives the bytes either way. A field the API sent with
    the wrong type reads as absent; in ``response_headers`` and ``scrape_do``
    an entry whose value is not a string is dropped.
    """

    task_id: str | None = None
    job_id: str | None = None
    url: str | None = None
    status: str | None = None
    status_code: int | None = None
    base64_encoded_content: bool = False
    response_headers: dict[str, str] | None = None
    content: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    expires_at: str | None = None
    update_time: str | None = None
    #: The gateway's own metadata for the task (the ``Scrape.do`` object).
    scrape_do: dict[str, str] | None = None
    error_message: str | None = None

    def content_bytes(self) -> bytes:
        """The content as bytes: base64-decoded when ``base64_encoded_content``
        is true, else UTF-8 encoded. Empty when there is no content.

        Raises :class:`~sdo.SdoTransportError` when base64 content is invalid
        (CR and LF line breaks are ignored).
        """
        if not self.content:
            return b""
        if not self.base64_encoded_content:
            # A lone surrogate (a "\\ud800" escape in the JSON) cannot be
            # UTF-8; it becomes U+FFFD, as it does in the JS and Go SDKs.
            return _SURROGATE_RE.sub("\ufffd", self.content).encode("utf-8")
        try:
            # CR and LF (MIME line wrapping) are ignored; anything else that is
            # not base64 is an error.
            wrapped = self.content.replace("\r", "").replace("\n", "")
            return base64.b64decode(wrapped, validate=True)
        except (binascii.Error, ValueError):
            raise SdoTransportError("The task content is not valid base64.") from None


@dataclass(kw_only=True)
class Account:
    """Account concurrency and credit snapshot from :meth:`AsyncJobs.me`."""

    total_concurrency: int | None = None
    free_concurrency: int | None = None
    active_jobs: int | None = None
    available_credits: int | None = None


def _str(d: Mapping[str, Any], key: str) -> str | None:
    value = d.get(key)
    return value if isinstance(value, str) else None


def _int(d: Mapping[str, Any], key: str) -> int | None:
    value = d.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_map(d: Mapping[str, Any], key: str) -> dict[str, str] | None:
    """A string map; an entry whose value is not a string is dropped."""
    value = d.get(key)
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if isinstance(v, str)}


def _list(d: Mapping[str, Any], key: str) -> builtins.list[Any]:
    value = d.get(key)
    return value if isinstance(value, list) else []


def _strings(d: Mapping[str, Any], key: str) -> builtins.list[str]:
    return [t for t in _list(d, key) if isinstance(t, str)]


def _task(d: Mapping[str, Any]) -> Task:
    return Task(task_id=_str(d, "TaskID"), url=_str(d, "URL"), status=_str(d, "Status"))


def _job_status(d: Mapping[str, Any]) -> JobStatus:
    return JobStatus(
        job_id=_str(d, "JobID"),
        task_ids=_strings(d, "TaskIDs"),
        status=_str(d, "Status"),
        start_time=_str(d, "StartTime"),
        end_time=_str(d, "EndTime"),
        acquired_concurrency=_int(d, "AcquiredConcurrency"),
        limit_concurrency=_int(d, "LimitConcurrency"),
        canceled=d.get("Canceled") is True,
        tasks=[_task(t) for t in _list(d, "Tasks") if isinstance(t, dict)],
    )


def _positive_int(value: object, what: str, maximum: int | None = None) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or (maximum is not None and value > maximum)
    ):
        bound = " up" if maximum is None else f" to {maximum}"
        raise SdoConfigError(f"{what} must be an integer from 1{bound}.")
    return str(value)


def _timestamp(value: object, what: str) -> str:
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            raise SdoConfigError(f"{what} must be timezone-aware (e.g. tzinfo=datetime.UTC).")
        return value.astimezone(_dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str) and _RFC3339_RE.match(value):
        return value
    raise SdoConfigError(
        f"{what} must be an aware datetime or an RFC 3339 string such as 2025-01-01T00:00:00Z."
    )


def task_result(d: Mapping[str, Any]) -> TaskResult:
    """Decode a task-result object; absent or mistyped fields are ``None``
    (``False`` for ``base64_encoded_content``)."""
    return TaskResult(
        task_id=_str(d, "TaskID"),
        job_id=_str(d, "JobID"),
        url=_str(d, "URL"),
        status=_str(d, "Status"),
        status_code=_int(d, "StatusCode"),
        base64_encoded_content=d.get("Base64EncodedContent") is True,
        response_headers=_str_map(d, "ResponseHeaders"),
        content=_str(d, "Content"),
        start_time=_str(d, "StartTime"),
        end_time=_str(d, "EndTime"),
        expires_at=_str(d, "ExpiresAt"),
        update_time=_str(d, "UpdateTime"),
        scrape_do=_str_map(d, "Scrape.do"),
        error_message=_str(d, "ErrorMessage"),
    )


def account(d: Mapping[str, Any]) -> Account:
    """Decode the account snapshot. The API spells the credits key
    ``AvaliableCredits``; the corrected ``AvailableCredits`` wins when present."""
    credits = _int(d, "AvailableCredits")
    return Account(
        total_concurrency=_int(d, "TotalConcurrency"),
        free_concurrency=_int(d, "FreeConcurrency"),
        active_jobs=_int(d, "ActiveJobs"),
        available_credits=credits if credits is not None else _int(d, "AvaliableCredits"),
    )


def _segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SdoConfigError(
            f"Invalid {name} {value!r}: ids are 1-128 letters, digits, '_' or '-'."
        )
    return quote(value, safe="")  # defence in depth; a valid id is unchanged


class AsyncJobs:
    """REST client for the ``q.scrape.do`` async job surface.

    Usually reached via :pyattr:`sdo.Client.jobs`, which shares the client's
    connection pool and settings. Constructing it directly builds a client
    with the same validation, environment fallbacks and safeguards; use it
    in a ``with`` block (or call :meth:`close`).

    The methods are synchronous; "async" is the name of the API (jobs that run
    in the background), not of an ``asyncio`` interface.

    :param token: API token. If omitted (or blank), ``SDO_TOKEN`` is read.
    :param base_url: job API base (default ``https://q.scrape.do``; env
        ``SDO_ASYNC_BASE_URL``).
    :param timeout: total timeout per request attempt, in seconds (default 60;
        env ``SDO_TIMEOUT``).
    :param retries: client-side retries (see :class:`~sdo.Client`).
    :param http_client: an ``httpx.Client`` to send through; not closed by
        :meth:`close`.
    :param allow_insecure_http: allow a plain ``http://`` base URL on a
        non-loopback host.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        retries: int = 2,
        http_client: httpx.Client | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        from .client import _DEFAULT_BASE_URL, Client

        secret = Secret(token)
        del token  # no frame keeps the raw value
        client = Client._from_secret(
            secret,
            base_url=_DEFAULT_BASE_URL,  # unused here; SDO_BASE_URL does not apply
            async_base_url=base_url,
            timeout=timeout,
            retries=retries,
            http_client=http_client,
            allow_insecure_http=allow_insecure_http,
        )
        self._init(client._transport, client.async_base_url, owns=True)

    @classmethod
    def _bound(cls, transport: Transport, *, base_url: str) -> AsyncJobs:
        self = cls.__new__(cls)
        self._init(transport, base_url, owns=False)
        return self

    def _init(self, transport: Transport, base_url: str, *, owns: bool) -> None:
        self._transport = transport
        self._api = f"{base_url}/api/v1"
        self._owns = owns

    def __repr__(self) -> str:
        api = getattr(self, "_api", None)  # None while half-built
        base = api.removesuffix("/api/v1") if api else None
        return f"AsyncJobs(base_url={base!r})"  # never the token

    def _send(self, method: str, path: str, *, body: str | None = None) -> bytes:
        headers = {"Content-Type": "application/json"} if body is not None else None
        content = body.encode("utf-8") if body is not None else None
        reply = self._transport.send(
            method, self._api + path, auth=HEADER, headers=headers, content=content, api=ASYNC
        )
        return reply.body

    def _object(self, method: str, path: str, *, body: str | None = None) -> dict[str, Any]:
        """The JSON object a successful call returns. An empty body, a body
        that is not JSON, or JSON that is not an object is a transport error."""
        raw = self._send(method, path, body=body)
        try:
            data = _json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise SdoTransportError(
                "The async API returned a body that is not a JSON object "
                f"({len(raw)} bytes from {method} {path})."
            )
        return data

    def create(self, targets: Sequence[str], options: Mapping[str, Any] | None = None) -> Job:
        """Submit a job. ``targets`` is a non-empty list of URLs; ``options`` is
        merged into the JSON body using the API's own PascalCase job-body keys
        (e.g. ``Super``, ``GeoCode``, ``Render``, ``Output``, ``WebhookURL``) and
        may set neither ``Targets`` nor ``Plugin``."""
        if isinstance(targets, str) or not isinstance(targets, (list, tuple)) or not targets:
            raise SdoConfigError("targets must be a non-empty list of URLs.")
        if not all(isinstance(t, str) and t for t in targets):
            raise SdoConfigError("Every target must be a non-empty URL string.")
        return self._create({"Targets": list(targets)}, options)

    def create_plugin(
        self,
        key: str,
        params: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any] | None = None,
    ) -> Job:
        """Submit a plugin job: ``key`` names the plugin (``amazon/pdp``,
        ``google/search``, ...) and each ``params`` entry becomes one task, with
        the plugin's own parameter names. 1 to 1000 entries. ``options`` are
        merged into the body like :meth:`create`'s and may set neither
        ``Targets`` nor ``Plugin``."""
        if not isinstance(key, str) or not _PLUGIN_KEY_RE.match(key):
            raise SdoConfigError(
                f"Invalid plugin key {key!r}: expected a plugin path such as amazon/pdp."
            )
        if not isinstance(params, (list, tuple)) or not 1 <= len(params) <= MAX_PLUGIN_PARAMS:
            raise SdoConfigError(
                f"A plugin job needs 1 to {MAX_PLUGIN_PARAMS} params entries (one per task)."
            )
        if not all(isinstance(p, Mapping) for p in params):
            raise SdoConfigError("Every plugin params entry must be a mapping.")
        source = {"Plugin": {"Key": key, "Params": [dict(p) for p in params]}}
        return self._create(source, options)

    def _create(self, source: dict[str, Any], options: Mapping[str, Any] | None) -> Job:
        if options is not None and not isinstance(options, Mapping):
            raise SdoConfigError(f"options must be a mapping, got {type(options).__name__}.")
        opts = dict(options or {})
        # Compared case-insensitively: a JSON decoder on the other side may be.
        for k in opts:
            if isinstance(k, str) and k.lower() in ("targets", "plugin"):
                raise SdoConfigError(
                    f"options may not set {k}: a job has either targets (create) "
                    "or a plugin (create_plugin)."
                )
        try:
            body = jdump({**opts, **source})
        except (TypeError, ValueError) as exc:
            raise SdoConfigError(f"options are not JSON-serializable: {exc}") from None
        d = self._object("POST", "/jobs", body=body)
        return Job(
            job_id=_str(d, "JobID"), message=_str(d, "Message"), task_ids=_strings(d, "TaskIDs")
        )

    def status(self, job_id: str) -> JobStatus:
        """Fetch a job's status and its per-target tasks."""
        return _job_status(self._object("GET", f"/jobs/{_segment(job_id, 'job_id')}"))

    def result(self, job_id: str, task_id: str) -> TaskResult:
        """Fetch the scraped result for one task within a job."""
        path = f"/jobs/{_segment(job_id, 'job_id')}/{_segment(task_id, 'task_id')}"
        return task_result(self._object("GET", path))

    def list(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
        status: str | None = None,
        start_from: _dt.datetime | str | None = None,
        start_to: _dt.datetime | str | None = None,
        sort: str | None = None,
    ) -> JobPage:
        """One page of the account's jobs (default: the 10 most recent).

        :param page: 1-based page number.
        :param page_size: jobs per page, 1-100.
        :param status: keep only jobs in this state (one of :data:`JOB_STATUSES`).
        :param start_from: inclusive lower bound on the job's start time: an
            aware ``datetime`` or an RFC 3339 string such as
            ``2025-01-01T00:00:00Z``.
        :param start_to: inclusive upper bound on the job's start time.
        :param sort: ``start_time_desc`` (default) or ``start_time_asc``.

        Walk every page with ``page`` from 1 to the returned ``total_pages``.
        """
        query: dict[str, str] = {}
        if page is not None:
            query["page"] = _positive_int(page, "page")
        if page_size is not None:
            query["page_size"] = _positive_int(page_size, "page_size", 100)
        if status is not None:
            if status not in JOB_STATUSES:
                raise SdoConfigError(f"status must be one of {', '.join(JOB_STATUSES)}.")
            query["status"] = status
        if start_from is not None:
            query["start_from"] = _timestamp(start_from, "start_from")
        if start_to is not None:
            query["start_to"] = _timestamp(start_to, "start_to")
        if sort is not None:
            if sort not in JOB_SORTS:
                raise SdoConfigError(f"sort must be one of {', '.join(JOB_SORTS)}.")
            query["sort"] = sort
        path = "/jobs" + (f"?{urlencode(query)}" if query else "")
        d = self._object("GET", path)
        return JobPage(
            jobs=[_job_status(j) for j in _list(d, "Jobs") if isinstance(j, dict)],
            total_count=_int(d, "TotalCount"),
            page_size=_int(d, "PageSize"),
            page_number=_int(d, "PageNumber"),
            total_pages=_int(d, "TotalPages"),
        )

    def cancel(self, job_id: str) -> None:
        """Cancel a job by id. Any 2xx is success, whatever its body says."""
        self._send("DELETE", f"/jobs/{_segment(job_id, 'job_id')}")

    def me(self) -> Account:
        """Fetch the account's concurrency and credit snapshot."""
        return account(self._object("GET", "/me"))

    def close(self) -> None:
        """Close the transport this instance created (a no-op for ``client.jobs``,
        which the client closes)."""
        if self._owns:
            self._transport.close()

    def __enter__(self) -> AsyncJobs:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["AsyncJobs", "Job", "JobStatus", "Task", "TaskResult", "Account"]
