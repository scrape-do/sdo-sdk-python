# SPDX-License-Identifier: Apache-2.0
"""Every README code block and every example runs as written, against a mock
gateway, so documentation cannot drift from the API."""

import pathlib
import re
import runpy

import httpx
import pytest
import respx

ROOT = pathlib.Path(__file__).resolve().parent.parent
USAGE = {
    "IsActive": True,
    "ConcurrentRequest": 40,
    "MaxMonthlyRequest": 3500000,
    "RemainingConcurrentRequest": 15,
    "RemainingMonthlyRequest": 2565023,
}
PAGE = b"<html><head><title>Example</title></head><body><h1>Hello</h1></body></html>"


def _gateway(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "api.scrape.do":
        if path.startswith("/plugin/"):
            return httpx.Response(200, json={"results": []})
        if path == "/info":
            return httpx.Response(200, json=USAGE)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})
    if path == "/api/v1/jobs" and request.method == "POST":
        return httpx.Response(200, json={"JobID": "j1", "TaskIDs": ["t1"]})
    if path == "/api/v1/jobs/j1" and request.method == "GET":
        tasks = [{"TaskID": "t1", "URL": "https://example.com", "Status": "success"}]
        return httpx.Response(200, json={"JobID": "j1", "Status": "success", "Tasks": tasks})
    if path == "/api/v1/jobs/j1/t1":
        return httpx.Response(200, json={"TaskID": "t1", "StatusCode": 200, "Content": "hi"})
    if path == "/api/v1/jobs" and request.method == "GET":
        return httpx.Response(200, json={"Jobs": []})
    return httpx.Response(200, json={})


def _readme_blocks():
    text = (ROOT / "README.md").read_text("utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("SDO_TOKEN", "doc-token")
    with respx.mock:
        respx.route().mock(side_effect=_gateway)
        yield


def test_readme_has_code_blocks():
    assert len(_readme_blocks()) >= 5


@pytest.mark.parametrize("index", range(len(_readme_blocks())))
def test_readme_block_runs(gateway, index):
    namespace = {"Client": __import__("sdo").Client}  # later blocks reuse the import
    code = _readme_blocks()[index].replace('Client(token="YOUR_TOKEN")', "Client()")
    exec(compile(code, f"README.md[{index}]", "exec"), namespace)


@pytest.mark.parametrize("path", sorted((ROOT / "examples").glob("*.py")), ids=lambda p: p.name)
def test_example_runs(gateway, path, capsys):
    runpy.run_path(str(path), run_name="__main__")
    assert capsys.readouterr().out
