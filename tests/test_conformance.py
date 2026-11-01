# SPDX-License-Identifier: Apache-2.0
"""Cross-language conformance: this SDK must match the shared generated corpus
(copied into this repo's tests by the codegen factory). Every section is
consumed here; a case whose ``skip`` names ``py`` is skipped, nothing else. The
corpus must match its sha256 sidecar, and a section, key or case field this
harness does not know fails the run (so a new expectation cannot be ignored)."""

import hashlib
import inspect
import json
import pathlib

import httpx
import pytest
import respx

from sdo import (
    Client,
    ScrapeConfig,
    SdoAPIError,
    SdoAuthError,
    SdoConcurrencyError,
    SdoConfigError,
    SdoGatewayError,
    SdoTransportError,
    SdoValidationError,
    __version__,
)
from sdo import _json as sdo_json
from sdo._transport import _BILLED_STATUS, effective_timeout, retry_after_seconds
from sdo.errors import redact_secrets
from sdo.jobs import task_result
from sdo.response import MAX_REQUEST_COST, ScrapeResult

_HERE = pathlib.Path(__file__).parent
CORPUS_BYTES = (_HERE / "conformance.json").read_bytes()
CORPUS = json.loads(CORPUS_BYTES.decode("utf-8"))
URL = CORPUS["url"]
SYNC = "https://api.scrape.do"
ASYNC = "https://q.scrape.do"

_KINDS = {
    "api": SdoAPIError,
    "auth": SdoAuthError,
    "validation": SdoValidationError,
    "concurrency": SdoConcurrencyError,
    "gateway": SdoGatewayError,
    "transport": SdoTransportError,
}


class Raw(httpx.SyncByteStream):
    """A body httpx has not read yet, so the SDK's own decoder handles it
    (``content=`` would make httpx decode it eagerly)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self):
        if self.data:
            yield self.data


def _cases(cases):
    """Parametrize over cases, skipping exactly those that list Python."""
    return [
        pytest.param(
            c,
            id=c.get("name") or c.get("route") or c.get("url") or c.get("id") or "case",
            marks=pytest.mark.skip(reason="input shape not expressible in Python")
            if "py" in c.get("skip", ())
            else (),
        )
        for c in cases
    ]


def _capture(route, status=200, **kw):
    """Mock ``route`` and keep every request it receives."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, **kw)

    route.mock(side_effect=handler)
    return seen


def _raw_query(request):
    return request.url.raw_path.decode("ascii").partition("?")[2]


# -- the corpus itself -------------------------------------------------------------

#: Every section, key and case field this harness consumes. Anything else in
#: the corpus is an expectation nobody asserts, so it fails the run.
_KNOWN = {
    "": {
        "version",
        "url",
        "contract",
        "serialization",
        "edge",
        "wire",
        "requests",
        "retry",
        "errors",
        "decoding",
        "decompression",
        "json_bodies",
        "redaction",
        "jobs",
        "client",
    },
    "contract": {
        "serialization",
        "decoding",
        "transport",
        "environment",
        "user_agent",
        "plugin_route_pattern",
        "redaction",
        "errors",
        "json_bodies",
    },
    "contract.serialization": {
        "omit_false_booleans",
        "omit_zero_values",
        "json_object_keys",
        "json_key_order",
        "json_html_escape",
        "json_line_separators",
        "numbers",
        "query_order",
        "extra_params_order",
        "extra_params_reserved",
        "enums",
    },
    "contract.decoding": {
        "algorithm",
        "bom_overrides_label",
        "strip_bom",
        "labels",
        "meta_sniff_bytes",
        "unknown_or_replacement_label",
        "meta_sniff_types",
        "invalid_bytes",
        "json_strips_bom",
        "max_decompressed_bytes",
        "max_response_bytes",
    },
    "contract.transport": {
        "client_timeout",
        "default_timeout_seconds",
        "effective_timeout",
        "gateway_timeout_margin_seconds",
        "follow_redirects",
        "https_only",
        "insecure_http_hosts",
        "base_url_parts",
        "empty_token_is_unset",
        "token_trim",
        "header_names",
        "header_values",
        "forbidden_headers",
        "content_codings",
        "retry",
    },
    "contract.transport.retry": {
        "default_retries",
        "retryable_statuses",
        "non_idempotent_methods",
        "non_idempotent_retry_statuses",
        "retry_after",
        "max_backoff_seconds",
        "negative_retries",
        "retry_after_send",
    },
    "contract.environment": {"SDO_TOKEN", "SDO_BASE_URL", "SDO_ASYNC_BASE_URL", "SDO_TIMEOUT"},
    "contract.user_agent": {"sdk", "cli", "pattern"},
    "contract.redaction": {"mask", "literal_token", "literal_min_length", "shapes"},
    "contract.errors": {
        "default_messages",
        "error_body_read_bytes",
        "async_billed",
        "billed_statuses",
        "target_origin",
        "billed",
        "request_cost",
        "envelope_keys",
    },
    "contract.errors.default_messages": {
        "validation",
        "auth",
        "concurrency",
        "gateway",
        "api",
        "redirect",
        "canceled",
    },
    "contract.json_bodies": {"invalid_utf8", "non_finite_numbers"},
    "wire": {"scrape", "plugin", "plugin_errors", "prefixed", "routes"},
    "retry": {"attempts", "retry_after"},
    "jobs": {
        "auth_header",
        "id_pattern",
        "task_results",
        "ids",
        "errors",
        "create",
        "account",
        "cancel_bodies",
    },
    "client": {
        "base_urls",
        "insecure_opt_in",
        "timeouts",
        "effective_timeouts",
        "tokens",
        "empty_token_falls_back_to_env",
        "target_urls",
    },
    "client.insecure_opt_in": {"url", "ok"},
}

#: The fields a case of each list section may carry.
_CASE_FIELDS = {
    "serialization": {"name", "py", "js", "go", "expect", "ordered", "error", "skip"},
    "edge": {"name", "py", "js", "go", "expect", "ordered", "error", "skip"},
    "wire.scrape": {"name", "py", "js", "go", "token", "path", "query", "skip"},
    "wire.plugin": {"name", "route", "params", "token", "path", "query", "skip"},
    "wire.plugin_errors": {"name", "route", "params", "skip"},
    "wire.prefixed": {
        "name",
        "base_url",
        "async_base_url",
        "scrape_path",
        "plugin_path",
        "jobs_path",
        "skip",
    },
    "wire.routes": {"route", "valid", "skip"},
    "requests": {
        "name",
        "method",
        "json",
        "data",
        "headers",
        "expect_method",
        "body",
        "content_type",
        "expect_headers",
        "expect_header_names",
        "ignore_header_names",
        "error",
        "skip",
    },
    "retry.attempts": {
        "name",
        "status",
        "headers",
        "method",
        "retries",
        "transparent",
        "attempts",
        "skip",
    },
    "retry.retry_after": {"value", "seconds", "skip"},
    "errors": {
        "name",
        "status",
        "headers",
        "body",
        "transparent",
        "kind",
        "billed",
        "retryable",
        "message",
        "retained_body_chars",
        "cost",
        "skip",
    },
    "decoding": {"name", "body_hex", "content_type", "text", "skip"},
    "decompression": {"name", "content_encoding", "body_hex", "text", "error", "attempts", "skip"},
    "json_bodies": {"name", "body_hex", "value", "error", "skip"},
    "redaction": {"name", "token", "input", "output", "skip"},
    "jobs.task_results": {"name", "json", "expect", "absent", "content_bytes_error", "skip"},
    "jobs.ids": {"id", "valid", "skip"},
    "jobs.errors": {"name", "status", "body", "kind", "message", "billed", "skip"},
    "jobs.create": {"name", "targets", "options", "body", "error", "skip"},
    "jobs.account": {"name", "json", "expect", "skip"},
    "client.base_urls": {"url", "ok", "skip"},
    "client.timeouts": {"value", "seconds", "skip"},
    "client.effective_timeouts": {"client_seconds", "config_timeout_ms", "seconds", "skip"},
    "client.tokens": {"value", "token", "skip"},
    "client.target_urls": {"url", "ok", "skip"},
}


def _node(path):
    node = CORPUS
    for part in filter(None, path.split(".")):
        node = node[part]
    return node


def test_corpus_matches_its_sha256_sidecar():
    """A hand edit of the corpus (e.g. deleting a red case) fails here."""
    sidecar = (_HERE / "conformance.json.sha256").read_text("ascii").split()[0]
    assert hashlib.sha256(CORPUS_BYTES).hexdigest() == sidecar


@pytest.mark.parametrize("path", sorted(_KNOWN))
def test_corpus_has_no_unknown_keys(path):
    assert set(_node(path)) == _KNOWN[path]


def test_corpus_list_sections_are_all_consumed():
    lists = set()
    for path in _KNOWN:
        for key, value in _node(path).items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                lists.add(f"{path}.{key}".lstrip("."))
    assert lists == set(_CASE_FIELDS)


@pytest.mark.parametrize("path", sorted(_CASE_FIELDS))
def test_corpus_cases_have_no_unknown_fields(path):
    for case in _node(path):
        assert set(case) <= _CASE_FIELDS[path], (path, case)


# -- serialization / edge -----------------------------------------------------


def _run_case(case):
    """``error: config`` may fire at construction or serialization; otherwise
    compare pairs (positionally when ``ordered``)."""
    if case.get("error") == "config":
        with pytest.raises(SdoConfigError):
            ScrapeConfig(url=URL, **case["py"]).to_params()
        return
    got = [tuple(p) for p in ScrapeConfig(url=URL, **case["py"]).to_params()]
    want = [tuple(p) for p in case["expect"]]
    if case.get("ordered"):
        assert got == want
    else:
        assert sorted(got) == sorted(want)


@pytest.mark.parametrize("case", _cases(CORPUS["serialization"]))
def test_serialization(case):
    _run_case(case)


@pytest.mark.parametrize("case", _cases(CORPUS["edge"]))
def test_edge(case):
    _run_case(case)


# -- wire: the exact bytes on the wire ---------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["wire"]["scrape"]))
def test_wire_scrape(case):
    with respx.mock:
        seen = _capture(respx.get(url__startswith=SYNC))
        with Client(token=case["token"], retries=0) as c:
            c.scrape(ScrapeConfig(url=URL, **case["py"]))
    (req,) = seen
    assert req.url.path == case["path"]
    assert _raw_query(req) == case["query"]


@pytest.mark.parametrize("case", _cases(CORPUS["wire"]["plugin"]))
def test_wire_plugin(case):
    with respx.mock:
        seen = _capture(respx.get(url__startswith=SYNC), json={})
        with Client(token=case["token"], retries=0) as c:
            c.plugin(case["route"], case["params"])
    (req,) = seen
    assert req.url.path == case["path"]
    assert _raw_query(req) == case["query"]


@pytest.mark.parametrize("case", _cases(CORPUS["wire"]["plugin_errors"]))
def test_wire_plugin_errors(case):
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={}))
        with Client(token="t", retries=0) as c, pytest.raises(SdoConfigError):
            c.plugin(case["route"], case["params"])
    assert mock.calls.call_count == 0


@pytest.mark.parametrize("case", _cases(CORPUS["wire"]["prefixed"]))
def test_wire_prefixed(case):
    with respx.mock:
        seen = _capture(respx.route(), json={})
        with Client(
            token="t", base_url=case["base_url"], async_base_url=case["async_base_url"], retries=0
        ) as c:
            c.scrape(URL)
            c.plugin("google/search")
            c.jobs.status("j-1")
    assert [r.url.path for r in seen] == [
        case["scrape_path"],
        case["plugin_path"],
        case["jobs_path"],
    ]


@pytest.mark.parametrize("case", _cases(CORPUS["wire"]["routes"]))
def test_wire_routes(case):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=SYNC).mock(return_value=httpx.Response(200, json={}))
        with Client(token="t", retries=0) as c:
            if case["valid"]:
                c.plugin(case["route"])
                assert mock.calls.call_count == 1
            else:
                with pytest.raises(SdoConfigError):
                    c.plugin(case["route"])
                assert mock.calls.call_count == 0


# -- requests: method, body, headers -------------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["requests"]))
def test_requests(case):
    kwargs = {k: case[k] for k in ("method", "json", "data", "headers") if k in case}
    with respx.mock(assert_all_called=False) as mock:
        seen = _capture(mock.route(url__startswith=SYNC))
        with Client(token="t", retries=2) as c:
            if case.get("error") == "config":
                with pytest.raises(SdoConfigError):
                    c.scrape(URL, **kwargs)
                assert mock.calls.call_count == 0
                return
            c.scrape(URL, **kwargs)
    (req,) = seen
    assert req.method == case["expect_method"]
    expected_body = b"" if case["body"] is None else case["body"].encode("utf-8")
    assert req.content == expected_body
    assert req.headers.get("content-type") == case["content_type"]
    for name, values in case.get("expect_headers", {}).items():
        assert req.headers.get_list(name) == values, name
    if "expect_header_names" in case:
        names = {k.decode("ascii").lower() for k, _ in req.headers.raw}
        names -= set(case["ignore_header_names"])  # framing the HTTP client manages
        assert sorted(names) == sorted(case["expect_header_names"])


# -- retry ----------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["retry"]["attempts"]))
def test_retry_attempts(case):
    method = case["method"]
    with respx.mock:
        route = respx.route(url__startswith=SYNC).mock(
            return_value=httpx.Response(case["status"], headers=case.get("headers", {}))
        )
        with Client(token="t", retries=case["retries"], backoff_factor=0.0) as c:
            data = None if method == "GET" else "x"
            try:
                c.scrape(
                    URL,
                    method=method,
                    data=data,
                    transparent_response=case.get("transparent", False),
                )
            except SdoAPIError:
                pass
    assert route.call_count == case["attempts"]


@pytest.mark.parametrize("case", _cases(CORPUS["retry"]["retry_after"]))
def test_retry_after(case):
    assert retry_after_seconds(case["value"]) == case["seconds"]


# -- errors -----------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["errors"]))
def test_errors(case):
    with respx.mock:
        respx.get(url__startswith=SYNC).mock(
            return_value=httpx.Response(
                case["status"], headers=case.get("headers", {}), text=case.get("body", "")
            )
        )
        with Client(token="t", retries=0, backoff_factor=0.0) as c:
            cfg = ScrapeConfig(url=URL, transparent_response=case.get("transparent", False))
            if case["kind"] is None:
                result = c.scrape(cfg)
                assert result.status_code == case["status"]
                raw = {k.lower(): v for k, v in case.get("headers", {}).items()}.get(
                    "scrape.do-request-cost"
                )
                if raw is not None:
                    assert result.cost == int(raw)
                if not case.get("transparent"):
                    c.plugin("google/search", {"q": "x"})  # a result, not an error
                return
            with pytest.raises(SdoAPIError) as ei:
                c.scrape(cfg)
            # A plugin call shares the status rules (it has no transparent mode).
            if not case.get("transparent"):
                with pytest.raises(SdoAPIError) as plugin_ei:
                    c.plugin("google/search", {"q": "x"})
                plugin_err = plugin_ei.value
                assert type(plugin_err) is type(ei.value)
                assert plugin_err.billed == ei.value.billed
                assert plugin_err.cost == ei.value.cost
                assert plugin_err.message == ei.value.message
    err = ei.value
    if case["kind"] == "api":
        assert type(err) is SdoAPIError
    else:
        assert isinstance(err, _KINDS[case["kind"]])
    assert err.status_code == case["status"]
    assert err.billed == case["billed"]
    assert err.is_retryable == case["retryable"]
    assert err.message == case["message"]
    if "cost" in case:  # null: no readable cost
        assert err.cost == case["cost"]
    if "retained_body_chars" in case:
        # The retained body is redacted, then truncated to 2048 characters.
        body = case.get("body", "")
        assert err.body == redact_secrets(body)[:2048]
        assert len(err.body) == case["retained_body_chars"]


# -- decoding ----------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["decoding"]))
def test_decoding(case):
    headers = {"content-type": case["content_type"]} if case["content_type"] else {}
    result = ScrapeResult(
        content=bytes.fromhex(case["body_hex"]), status_code=200, headers=headers, url=URL
    )
    assert result.text == case["text"]


# -- decompression (through the real transport path) ---------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["decompression"]))
def test_decompression(case):
    with respx.mock:
        route = respx.get(url__startswith=SYNC).mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Encoding": case["content_encoding"]},
                stream=Raw(bytes.fromhex(case["body_hex"])),
            )
        )
        with Client(token="t", retries=2, backoff_factor=0.0) as c:
            if "error" in case:
                with pytest.raises(_KINDS[case["error"]]):
                    c.scrape(URL)
            else:
                assert c.scrape(URL).text == case["text"]
    assert route.call_count == case.get("attempts", 1)


# -- json_bodies: result.json(), plugin and job JSON ---------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["json_bodies"]))
def test_json_bodies_result(case):
    result = ScrapeResult(
        content=bytes.fromhex(case["body_hex"]), status_code=200, headers={}, url=URL
    )
    if "error" in case:
        with pytest.raises(_KINDS[case["error"]]):
            result.json()
    else:
        assert result.json() == case["value"]


@pytest.mark.parametrize("case", _cases(CORPUS["json_bodies"]))
def test_json_bodies_plugin(case):
    body = bytes.fromhex(case["body_hex"])
    with respx.mock:
        respx.get(url__startswith=SYNC).mock(
            return_value=httpx.Response(
                200, headers={"Content-Type": "application/json"}, stream=Raw(body)
            )
        )
        with Client(token="t", retries=0) as c:
            if "error" in case:
                with pytest.raises(_KINDS[case["error"]]):
                    c.plugin("google/search")
            else:
                assert c.plugin("google/search") == case["value"]


@pytest.mark.parametrize("case", _cases(CORPUS["json_bodies"]))
def test_json_bodies_jobs(case):
    """A job response must also be a JSON object; anything else is a
    transport error there."""
    body = bytes.fromhex(case["body_hex"])
    with respx.mock:
        respx.get(f"{ASYNC}/api/v1/jobs/job-1").mock(
            return_value=httpx.Response(200, stream=Raw(body))
        )
        with Client(token="t", retries=0) as c:
            if "error" in case or not isinstance(case["value"], dict):
                with pytest.raises(SdoTransportError):
                    c.jobs.status("job-1")
            else:
                assert sdo_json.loads(body) == case["value"]
                c.jobs.status("job-1")


# -- redaction ------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["redaction"]))
def test_redaction(case):
    assert redact_secrets(case["input"], case["token"]) == case["output"]


# -- jobs ------------------------------------------------------------------------------


def test_jobs_constants():
    from sdo.jobs import _ID_RE

    assert CORPUS["jobs"]["auth_header"] == "X-Token"
    assert _ID_RE.pattern == CORPUS["jobs"]["id_pattern"]


@pytest.mark.parametrize("case", _cases(CORPUS["jobs"]["task_results"]))
def test_jobs_task_results(case):
    result = task_result(case["json"])
    for key, want in case["expect"].items():
        if key == "content_bytes_hex":
            assert result.content_bytes().hex() == want
        else:
            assert getattr(result, key) == want, key
    for key in case.get("absent", ()):
        assert getattr(result, key) is None, key
    if "content_bytes_error" in case:
        with pytest.raises(_KINDS[case["content_bytes_error"]]):
            result.content_bytes()


@pytest.mark.parametrize("case", _cases(CORPUS["jobs"]["ids"]))
def test_jobs_ids(case):
    job_id = case["id"]
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__startswith=ASYNC).mock(return_value=httpx.Response(200, json={}))
        with Client(token="t", retries=0) as c:
            calls = (
                lambda: c.jobs.status(job_id),
                lambda: c.jobs.result(job_id, "task-1"),
                lambda: c.jobs.result("job-1", job_id),
                lambda: c.jobs.cancel(job_id),
            )
            for call in calls:
                if case["valid"]:
                    call()
                else:
                    with pytest.raises(SdoConfigError):
                        call()
        assert mock.calls.call_count == (len(calls) if case["valid"] else 0)


@pytest.mark.parametrize("case", _cases(CORPUS["jobs"]["errors"]))
def test_jobs_errors(case):
    with respx.mock:
        route = respx.get(f"{ASYNC}/api/v1/jobs/job-1").mock(
            return_value=httpx.Response(
                case["status"], text=case["body"], headers={"Location": "https://elsewhere.test/"}
            )
        )
        with Client(token="t", retries=0) as c, pytest.raises(_KINDS[case["kind"]]) as ei:
            c.jobs.status("job-1")
    assert route.call_count == 1  # a redirect is never followed
    err = ei.value
    if case["kind"] == "api":
        assert type(err) is SdoAPIError
    if case["message"] is not None:
        assert err.message == case["message"]
    if "billed" in case:
        assert err.billed is case["billed"]


@pytest.mark.parametrize("case", _cases(CORPUS["jobs"]["create"]))
def test_jobs_create(case):
    with respx.mock(assert_all_called=False) as mock:
        seen = _capture(mock.post(f"{ASYNC}/api/v1/jobs"), json={"JobID": "j"})
        with Client(token="t", retries=0) as c:
            if case.get("error") == "config":
                with pytest.raises(SdoConfigError):
                    c.jobs.create(case["targets"], case["options"])
                assert mock.calls.call_count == 0
                return
            c.jobs.create(case["targets"], case["options"])
    (req,) = seen
    assert req.content == case["body"].encode("utf-8")
    assert req.headers["x-token"] == "t"
    assert "token" not in req.url.params


@pytest.mark.parametrize("case", _cases(CORPUS["jobs"]["account"]))
def test_jobs_account(case):
    with respx.mock:
        respx.get(f"{ASYNC}/api/v1/me").mock(return_value=httpx.Response(200, json=case["json"]))
        with Client(token="t", retries=0) as c:
            account = c.jobs.me()
    for key, want in case["expect"].items():
        assert getattr(account, key) == want, key


@pytest.mark.parametrize("body", CORPUS["jobs"]["cancel_bodies"])
def test_jobs_cancel_ignores_the_body(body):
    with respx.mock:
        route = respx.delete(f"{ASYNC}/api/v1/jobs/job-1").mock(
            return_value=httpx.Response(200, text=body)
        )
        with Client(token="t", retries=0) as c:
            assert c.jobs.cancel("job-1") is None
    assert route.call_count == 1


# -- client ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(CORPUS["client"]["base_urls"]))
def test_client_base_urls(case):
    for kwarg in ("base_url", "async_base_url"):
        if case["ok"]:
            Client(token="t", **{kwarg: case["url"]}).close()
        else:
            with pytest.raises(SdoConfigError):
                Client(token="t", **{kwarg: case["url"]})


@pytest.mark.parametrize("case", _cases(CORPUS["client"]["base_urls"]))
def test_client_base_urls_from_environment(case, monkeypatch):
    for env in ("SDO_BASE_URL", "SDO_ASYNC_BASE_URL"):
        monkeypatch.setenv(env, case["url"])
        if case["ok"]:
            Client(token="t").close()
        else:
            with pytest.raises(SdoConfigError):
                Client(token="t")
        monkeypatch.delenv(env)


def test_client_insecure_opt_in():
    case = CORPUS["client"]["insecure_opt_in"]
    for kwarg in ("base_url", "async_base_url"):
        with pytest.raises(SdoConfigError):
            Client(token="t", **{kwarg: case["url"]})
        c = Client(token="t", allow_insecure_http=True, **{kwarg: case["url"]})
        assert case["ok"] is True
        c.close()


@pytest.mark.parametrize("case", _cases(CORPUS["client"]["timeouts"]))
def test_client_timeouts(case, monkeypatch):
    monkeypatch.setenv("SDO_TIMEOUT", case["value"])
    if case["seconds"] is None:
        with pytest.raises(SdoConfigError):
            Client(token="t")
    else:
        with Client(token="t") as c:
            assert c._timeout == case["seconds"]


@pytest.mark.parametrize("case", _cases(CORPUS["client"]["effective_timeouts"]))
def test_client_effective_timeouts(case):
    """The computation, and the timeout the request actually carries."""
    ms = case["config_timeout_ms"]
    assert effective_timeout(case["client_seconds"], ms) == case["seconds"]
    with respx.mock:
        seen = _capture(respx.get(url__startswith=SYNC))
        with Client(token="t", timeout=case["client_seconds"], retries=0) as c:
            c.scrape(URL, timeout=ms)
    assert seen[0].extensions["timeout"]["read"] == case["seconds"]


@pytest.mark.parametrize("case", _cases(CORPUS["client"]["tokens"]))
def test_client_tokens(case, monkeypatch):
    """Trimmed; blank means "not given" (falls back to SDO_TOKEN); inner
    whitespace or a control character is a config error."""
    monkeypatch.setenv("SDO_TOKEN", "from-env")
    want = case["token"]
    if want is None:
        with pytest.raises(SdoConfigError):
            Client(token=case["value"])
        return
    with respx.mock:
        seen = _capture(respx.get(url__startswith=SYNC))
        with Client(token=case["value"], retries=0) as c:
            c.scrape(URL)
    assert seen[0].url.params["token"] == (want or "from-env")


def test_client_empty_token_falls_back_to_env(monkeypatch):
    assert CORPUS["client"]["empty_token_falls_back_to_env"] is True
    monkeypatch.setenv("SDO_TOKEN", "from-env")
    with respx.mock:
        seen = _capture(respx.get(url__startswith=SYNC))
        with Client(token="") as c:
            c.scrape(URL)
    assert seen[0].url.params["token"] == "from-env"
    monkeypatch.delenv("SDO_TOKEN")
    with pytest.raises(SdoConfigError):
        Client(token="")


@pytest.mark.parametrize("case", _cases(CORPUS["client"]["target_urls"]))
def test_client_target_urls(case):
    with respx.mock(assert_all_called=False) as mock:
        seen = _capture(mock.get(url__startswith=SYNC))
        with Client(token="t", retries=0) as c:
            if case["ok"]:
                c.scrape(case["url"])
            else:
                with pytest.raises(SdoConfigError):
                    ScrapeConfig(url=case["url"])
                with pytest.raises(SdoConfigError):
                    c.scrape(case["url"])
    if case["ok"]:
        assert seen[0].url.params["url"] == case["url"]  # sent exactly as given
    else:
        assert mock.calls.call_count == 0


# -- contract ---------------------------------------------------------------------------


def test_contract_constants_match_implementation():
    """The corpus carries the normative contract; the SDK must implement it."""
    import re

    from sdo._decompress import codings
    from sdo._transport import (
        _DEFAULT_MESSAGES,
        _ERROR_BODY_BYTES,
        _FORBIDDEN_HEADERS,
        _GATEWAY_TIMEOUT_MARGIN,
        _MAX_BACKOFF,
        _MAX_BYTES,
        _NON_IDEMPOTENT,
        _NON_IDEMPOTENT_RETRY_STATUS,
        _RETRYABLE_STATUS,
    )
    from sdo.client import _DEFAULT_TIMEOUT, _LOOPBACK, _user_agent
    from sdo.errors import _LITERAL_MIN_LENGTH, _MASK
    from sdo.plugins import _ROUTE_RE

    contract = CORPUS["contract"]
    ser = contract["serialization"]
    assert ser["omit_false_booleans"] is True and ser["omit_zero_values"] is True
    assert ser["json_object_keys"] == "sorted" and ser["json_key_order"] == "code_point"
    assert ser["json_html_escape"] is False and ser["numbers"] == "ecmascript"
    assert ser["json_line_separators"] == "literal" and ser["enums"] == "lowercase"
    assert ser["query_order"] == "spec_then_extra_sorted" and ser["extra_params_order"] == "sorted"
    assert ser["extra_params_reserved"] == ["token", "url"]
    dec = contract["decoding"]
    assert _MAX_BYTES == dec["max_decompressed_bytes"] == dec["max_response_bytes"]
    assert dec["meta_sniff_bytes"] == 1024 and dec["algorithm"] == "whatwg"
    assert dec["labels"] == "whatwg" and dec["unknown_or_replacement_label"] == "ignored"
    assert dec["meta_sniff_types"] == ["", "text/html", "application/xhtml+xml"]
    assert dec["invalid_bytes"] == "replace" and dec["json_strips_bom"] is True
    assert dec["bom_overrides_label"] is True and dec["strip_bom"] is True
    tr = contract["transport"]
    assert tr["client_timeout"] == "total"
    assert tr["default_timeout_seconds"] == _DEFAULT_TIMEOUT
    assert tr["gateway_timeout_margin_seconds"] == _GATEWAY_TIMEOUT_MARGIN
    assert tr["effective_timeout"].startswith("max(client_timeout, config.timeout_ms / 1000")
    assert tr["follow_redirects"] is False and tr["https_only"] is True
    assert tuple(tr["insecure_http_hosts"]) == _LOOPBACK
    assert tr["base_url_parts"] == ["scheme", "host", "port", "path_prefix"]
    assert tr["empty_token_is_unset"] is True and tr["token_trim"] == "ascii_whitespace"
    assert tr["header_names"] == "rfc9110_token" and tr["header_values"] == "printable_ascii"
    assert set(tr["forbidden_headers"]) == _FORBIDDEN_HEADERS
    assert codings(", ".join(tr["content_codings"])) == ["br", "deflate", "gzip", "gzip"]
    retry = tr["retry"]
    default_retries = inspect.signature(Client).parameters["retries"].default
    assert retry["default_retries"] == default_retries
    assert set(retry["retryable_statuses"]) == _RETRYABLE_STATUS
    assert set(retry["non_idempotent_methods"]) == _NON_IDEMPOTENT
    assert set(retry["non_idempotent_retry_statuses"]) == _NON_IDEMPOTENT_RETRY_STATUS
    assert retry["max_backoff_seconds"] == _MAX_BACKOFF and retry["retry_after"] == "seconds"
    assert retry["negative_retries"] == "clamp_to_zero" and retry["retry_after_send"] is False
    assert set(contract["environment"]) == {
        "SDO_TOKEN",
        "SDO_BASE_URL",
        "SDO_ASYNC_BASE_URL",
        "SDO_TIMEOUT",
    }
    red = contract["redaction"]
    assert red["mask"] == _MASK and red["literal_min_length"] == _LITERAL_MIN_LENGTH
    assert red["literal_token"] is True
    assert red["shapes"] == ["query_or_text", "json_field", "x_token_header"]
    assert _ROUTE_RE.pattern == contract["plugin_route_pattern"]
    ua = contract["user_agent"]
    assert _user_agent() == ua["sdk"].replace("<lang>", "python").replace("<version>", __version__)
    assert re.fullmatch(ua["pattern"], _user_agent())
    errors = contract["errors"]
    assert errors["default_messages"] == _DEFAULT_MESSAGES
    assert errors["error_body_read_bytes"] == _ERROR_BODY_BYTES
    assert errors["async_billed"] is False
    assert errors["billed_statuses"] == sorted(_BILLED_STATUS)
    # Pinned by the errors cases; the names are the rules target_answered and
    # build_api_error implement.
    assert errors["target_origin"] == "request_cost_positive"
    assert errors["billed"] == "request_cost_else_billed_statuses"
    assert errors["request_cost"] == {
        "values": 1,
        "trim": " \t",
        "digits": "ascii",
        "max": MAX_REQUEST_COST,
    }
    assert errors["envelope_keys"] == ["message", "error"]
    assert contract["json_bodies"] == {"invalid_utf8": "replace", "non_finite_numbers": "reject"}
