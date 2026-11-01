# SPDX-License-Identifier: Apache-2.0
"""Async job REST API (q.scrape.do/api/v1) — mocked, no live calls.

Every jobs call authenticates via the ``X-Token`` header (never a token= query),
targets the ``/api/v1/...`` REST surface, and returns typed dataclasses.
"""

import gzip
import json

import httpx
import pytest
import respx

from sdo import (
    Account,
    AsyncJobs,
    Client,
    Job,
    JobStatus,
    SdoAPIError,
    SdoConfigError,
    SdoConnectionError,
    SdoResponseTooLargeError,
    SdoTimeoutError,
    SdoTransportError,
    SdoValidationError,
    TaskResult,
)
from sdo.client import _user_agent

BASE = "https://q.scrape.do"


@respx.mock
def test_jobs_create_posts_canonical_json_with_x_token_header():
    route = respx.post(f"{BASE}/api/v1/jobs").mock(
        return_value=httpx.Response(
            200, json={"JobID": "job-1", "Message": "Job created successfully", "TaskIDs": ["t-1"]}
        )
    )
    with Client(token="t") as c:
        job = c.jobs.create(["https://e.com"], {"Super": True, "GeoCode": "us"})

    assert job == Job(job_id="job-1", message="Job created successfully", task_ids=["t-1"])
    req = route.calls.last.request
    assert req.method == "POST"
    assert req.headers["X-Token"] == "t"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["user-agent"] == _user_agent()
    assert "token" not in req.url.params  # token rides the header, not the query
    assert req.content == b'{"GeoCode":"us","Super":true,"Targets":["https://e.com"]}'


@pytest.mark.parametrize(
    ("targets", "options"),
    [
        ("https://e.com", None),  # a bare string is not a list of targets
        ([], None),
        ([""], None),
        (["https://e.com"], {"targets": ["x"]}),
        (["https://e.com"], {"Render": object()}),
    ],
)
def test_jobs_create_rejects_bad_input_before_sending(targets, options):
    with respx.mock(assert_all_called=False) as mock, Client(token="t") as c:
        with pytest.raises(SdoConfigError):
            c.jobs.create(targets, options)
        assert mock.calls.call_count == 0


@respx.mock
def test_jobs_status_parses_tasks():
    route = respx.get(f"{BASE}/api/v1/jobs/job-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "JobID": "job-1",
                "TaskIDs": ["t-1", 7],
                "Status": "success",
                "StartTime": "2026-07-16T00:00:00Z",
                "EndTime": "2026-07-16T00:00:02Z",
                "Canceled": False,
                "Tasks": [{"TaskID": "t-1", "URL": "https://e.com", "Status": "success"}, "junk"],
            },
        )
    )
    with Client(token="t") as c:
        st = c.jobs.status("job-1")

    assert isinstance(st, JobStatus)
    assert (st.status, st.canceled, st.task_ids) == ("success", False, ["t-1"])
    assert len(st.tasks) == 1 and st.tasks[0].url == "https://e.com"
    assert route.calls.last.request.headers["X-Token"] == "t"


@respx.mock
def test_jobs_result_parses_task_result():
    respx.get(f"{BASE}/api/v1/jobs/job-1/t-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "TaskID": "t-1",
                "JobID": "job-1",
                "URL": "https://e.com",
                "Status": "success",
                "StatusCode": 200,
                "Base64EncodedContent": False,
                "ResponseHeaders": {"content-type": "text/html"},
                "Content": "<html></html>",
            },
        )
    )
    with Client(token="t") as c:
        res = c.jobs.result("job-1", "t-1")

    assert isinstance(res, TaskResult)
    assert res.status_code == 200
    assert res.base64_encoded_content is False
    assert res.content_bytes() == b"<html></html>"
    assert res.response_headers == {"content-type": "text/html"}


def test_task_result_content_bytes_edge_cases():
    assert TaskResult().content_bytes() == b""
    assert TaskResult(content="aGk=", base64_encoded_content=True).content_bytes() == b"hi"
    with pytest.raises(SdoTransportError, match="base64"):
        TaskResult(content="not base64!", base64_encoded_content=True).content_bytes()


@respx.mock
def test_jobs_list_unwraps_jobs_array():
    respx.get(f"{BASE}/api/v1/jobs").mock(
        return_value=httpx.Response(
            200, json={"Jobs": [{"JobID": "a", "Status": "success"}, {"JobID": "b"}, 3]}
        )
    )
    with Client(token="t") as c:
        jobs = c.jobs.list()
    assert [j.job_id for j in jobs] == ["a", "b"]


@respx.mock
def test_jobs_cancel_issues_delete_and_accepts_an_empty_body():
    route = respx.delete(f"{BASE}/api/v1/jobs/job-1").mock(return_value=httpx.Response(200))
    with Client(token="t") as c:
        assert c.jobs.cancel("job-1") is None
    assert route.calls.last.request.headers["X-Token"] == "t"


@respx.mock
def test_jobs_me_maps_available_credits():
    route = respx.get(f"{BASE}/api/v1/me")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "TotalConcurrency": 5000,
                "FreeConcurrency": 2500,
                "ActiveJobs": 0,
                "AvaliableCredits": 149,  # the API's real (misspelled) key
            },
        ),
        httpx.Response(200, json={"AvailableCredits": 150, "ActiveJobs": True}),
    ]
    with Client(token="t") as c:
        acct = c.jobs.me()
        fixed = c.jobs.me()
    assert acct == Account(
        total_concurrency=5000, free_concurrency=2500, active_jobs=0, available_credits=149
    )
    assert (fixed.available_credits, fixed.active_jobs) == (150, None)


@respx.mock
def test_jobs_error_envelope_reads_only_error():
    route = respx.post(f"{BASE}/api/v1/jobs")
    route.side_effect = [
        httpx.Response(400, json={"Error": "Targets is required", "Code": 400}),
        httpx.Response(400, json={"Message": ["Please check the 'Targets' parameter."]}),
    ]
    with Client(token="t") as c:
        with pytest.raises(SdoValidationError, match="Targets is required") as ei:
            c.jobs.create(["https://e.com"])
        assert ei.value.billed is False  # the async API's errors are not billed
        with pytest.raises(SdoValidationError) as ei:
            c.jobs.create(["https://e.com"])
        assert ei.value.message == "The gateway rejected the request parameters."


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"", b"  ", b"{"])
def test_jobs_unexpected_json_shape_is_a_transport_error(body):
    with respx.mock:
        route = respx.get(f"{BASE}/api/v1/me").mock(return_value=httpx.Response(200, content=body))
        with Client(token="t") as c, pytest.raises(SdoTransportError, match="JSON object"):
            c.jobs.me()
    assert route.call_count == 1  # a 2xx was delivered: not retried


@respx.mock
def test_jobs_body_is_capped_like_scrape():
    """The jobs path used to read and inflate the whole body with no ceiling."""
    bomb = gzip.compress(b'{"Content":"' + b"a" * (4 * 1024 * 1024) + b'"}')
    respx.get(f"{BASE}/api/v1/jobs/j/t").mock(
        return_value=httpx.Response(
            200, stream=httpx.ByteStream(bomb), headers={"Content-Encoding": "gzip"}
        )
    )
    with Client(token="t", max_response_bytes=1024 * 1024) as c:
        with pytest.raises(SdoResponseTooLargeError):
            c.jobs.result("j", "t")


@respx.mock
def test_jobs_get_is_retried_but_create_only_on_429():
    get = respx.get(f"{BASE}/api/v1/me").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={})]
    )
    post = respx.post(f"{BASE}/api/v1/jobs").mock(return_value=httpx.Response(503))
    with Client(token="t", backoff_factor=0.0) as c:
        c.jobs.me()
        with pytest.raises(SdoAPIError):
            c.jobs.create(["https://e.com"])
    assert (get.call_count, post.call_count) == (2, 1)


@respx.mock
def test_jobs_transport_failures_are_typed_and_redacted():
    jobs = Client(token="SUPERSECRETTOKEN", retries=0).jobs
    respx.get(f"{BASE}/api/v1/me").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(SdoTimeoutError):
        jobs.me()
    respx.get(f"{BASE}/api/v1/me").mock(
        side_effect=httpx.ConnectError("refused X-Token: SUPERSECRETTOKEN")
    )
    with pytest.raises(SdoConnectionError) as ei:
        jobs.me()
    assert "SUPERSECRETTOKEN" not in str(ei.value)


def test_async_jobs_can_be_constructed_directly(monkeypatch):
    """The direct constructor goes through the same validation, environment
    fallbacks and User-Agent as Client."""
    from sdo.client import _user_agent

    with respx.mock:
        route = respx.get("https://jobs.example.com/api/v1/me").mock(
            return_value=httpx.Response(200, json={"ActiveJobs": 1})
        )
        with AsyncJobs(token=" t\n", base_url="https://jobs.example.com/", timeout=5) as jobs:
            assert jobs.me().active_jobs == 1
            assert repr(jobs) == "AsyncJobs(base_url='https://jobs.example.com')"
        with pytest.raises(SdoConfigError, match="closed"):
            jobs.me()
    sent = route.calls.last.request
    assert sent.headers["user-agent"] == _user_agent()
    assert sent.headers["x-token"] == "t"
    assert sent.extensions["timeout"]["read"] == 5
    for bad in (
        {"base_url": "http://jobs.example.com"},
        {"timeout": -1},
        {"timeout": float("nan")},
        {"retries": "2"},
    ):
        with pytest.raises(SdoConfigError):
            AsyncJobs(token="t", **bad)
    with pytest.raises(SdoConfigError, match="token"):
        AsyncJobs(token="bad token")
    monkeypatch.setenv("SDO_TOKEN", "from-env")
    monkeypatch.setenv("SDO_ASYNC_BASE_URL", "https://q.example")
    with respx.mock:
        route = respx.get("https://q.example/api/v1/me").mock(
            return_value=httpx.Response(200, json={})
        )
        with AsyncJobs() as jobs:
            jobs.me()
    assert route.calls.last.request.headers["x-token"] == "from-env"


def test_client_jobs_is_shared_and_closed_with_the_client():
    c = Client(token="t")
    assert c.jobs is c.jobs
    c.jobs.close()  # owned by the client: a no-op
    with respx.mock:
        respx.get(f"{BASE}/api/v1/me").mock(return_value=httpx.Response(200, json={}))
        c.jobs.me()
    c.close()
    with pytest.raises(SdoConfigError):
        c.jobs.me()


def test_job_body_is_canonical_json():
    """Same encoder as ScrapeConfig JSON params: sorted keys, ES numbers."""
    with respx.mock:
        route = respx.post(f"{BASE}/api/v1/jobs").mock(return_value=httpx.Response(200, json={}))
        with Client(token="t") as c:
            c.jobs.create(["https://e.com"], {"Wait": 1.0, "A": " "})
    assert json.loads(route.calls.last.request.content) == {
        "A": " ",
        "Targets": ["https://e.com"],
        "Wait": 1,
    }
    assert route.calls.last.request.content.startswith(b'{"A":"\xe2\x80\xa8","Targets"')
