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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

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
    canceled: bool = False
    tasks: list[Task] = field(default_factory=list)


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
        canceled=d.get("Canceled") is True,
        tasks=[_task(t) for t in _list(d, "Tasks") if isinstance(t, dict)],
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
        (e.g. ``Super``, ``GeoCode``, ``Render``, ``Output``)."""
        if isinstance(targets, str) or not isinstance(targets, (list, tuple)) or not targets:
            raise SdoConfigError("targets must be a non-empty list of URLs.")
        if not all(isinstance(t, str) and t for t in targets):
            raise SdoConfigError("Every target must be a non-empty URL string.")
        if options is not None and not isinstance(options, Mapping):
            raise SdoConfigError(f"options must be a mapping, got {type(options).__name__}.")
        opts = dict(options or {})
        if any(isinstance(k, str) and k.lower() == "targets" for k in opts):
            raise SdoConfigError("Pass the targets as the first argument, not inside options.")
        try:
            body = jdump({**opts, "Targets": list(targets)})
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

    def list(self) -> builtins.list[JobStatus]:
        """List all jobs for the account."""
        d = self._object("GET", "/jobs")
        return [_job_status(j) for j in _list(d, "Jobs") if isinstance(j, dict)]

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
