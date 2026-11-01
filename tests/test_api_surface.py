# SPDX-License-Identifier: Apache-2.0
"""The committed api-surface.txt matches the package, and follows the golden
grammar a release's SemVer bump is derived from."""

import importlib.util
import pathlib
import re

import sdo

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("api_surface", ROOT / "tools" / "api_surface.py")
api_surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_surface)

_LINE = re.compile(
    r"(export|data|class|base|ctor|method|func|prop|attr|param|staticmethod|classmethod) \S+( .+)?"
)


def test_committed_golden_is_current():
    """Mirrors the CI drift gate: regenerate with
    ``python tools/api_surface.py > api-surface.txt``."""
    assert (ROOT / "api-surface.txt").read_text("utf-8") == api_surface.build_surface()


def test_golden_grammar():
    lines = [ln for ln in api_surface.build_surface().splitlines() if not ln.startswith("#")]
    assert lines == sorted(set(lines)), "sorted and unique"
    for line in lines:
        assert _LINE.fullmatch(line), line
        assert "  " not in line and "(none)" not in line
    assert sdo.__version__ not in "\n".join(lines)  # no release-varying values
    assert "data __version__: str" in lines
    # every parameter on its own line, positional ones numbered, required marked
    assert "param Client.scrape #0 config: ScrapeConfig | str | None = None" in lines
    assert "param AsyncJobs.result #1 task_id: str [required]" in lines
    assert "param Client.plugin **kwargs: Any" in lines
    assert "base SdoAuthError SdoAPIError" in lines
    assert "attr TaskResult.base64_encoded_content: bool" in lines
    assert "prop ScrapeResult.text: str" in lines
    # Only url is positional: a new ScrapeConfig field in any position is additive.
    config = [ln for ln in lines if ln.startswith("param ScrapeConfig.__init__ ")]
    assert [ln for ln in config if " #" in ln] == [
        "param ScrapeConfig.__init__ #0 url: str [required]"
    ]
    assert "param ScrapeConfig.__init__ render: bool | str | None = None" in config


def test_extractor_handles_functions_and_static_members(monkeypatch):
    class Probe:
        __module__ = "sdo.probe"

        @staticmethod
        def make(a: int, /, b, *args, c: str = "x", **kw) -> "Probe":
            return Probe()

        @classmethod
        def build(cls, value: int) -> "Probe":
            return cls()

        @property
        def untyped(self):
            return 1

    def helper(x: int, *, y: bool = False) -> None:
        return None

    helper.__module__ = "sdo"
    monkeypatch.setattr(sdo, "__all__", ["Probe", "helper"])
    monkeypatch.setattr(sdo, "Probe", Probe, raising=False)
    monkeypatch.setattr(sdo, "helper", helper, raising=False)
    lines = api_surface.build_surface().splitlines()
    for expected in (
        "staticmethod Probe.make() -> Probe",
        "param Probe.make #0 a: int [required]",
        "param Probe.make #1 b [required]",
        "param Probe.make *args",
        "param Probe.make c: str = 'x'",
        "param Probe.make **kw",
        "classmethod Probe.build() -> Probe",
        "param Probe.build #0 value: int [required]",
        "prop Probe.untyped",
        "func helper() -> None",
        "param helper #0 x: int [required]",
        "param helper y: bool = False",
    ):
        assert expected in lines, expected
    assert api_surface._ann(int) == "int"


def test_public_annotations_resolve_at_runtime():
    """Runtime-typing libraries (pydantic, cattrs, typeguard) call
    get_type_hints on the public types; every annotation must resolve."""
    import inspect
    import typing

    for name in sdo.__all__:
        obj = getattr(sdo, name)
        if not inspect.isclass(obj):
            continue
        typing.get_type_hints(obj)
        for _, member in inspect.getmembers(obj):
            func = member.fget if isinstance(member, property) else member
            if inspect.isfunction(func) and func.__module__.startswith("sdo"):
                typing.get_type_hints(func)


def test_pinned_requirements_track_pyproject():
    """requirements-*.in repeat two pins from pyproject.toml so CI can install
    everything with --require-hashes; a bump of one must bump the other."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    dev_in = (ROOT / "requirements-dev.in").read_text("utf-8").split()
    for requirement in project["project"]["dependencies"]:
        assert requirement in dev_in, requirement
    (backend,) = project["build-system"]["requires"]
    for name in ("requirements-dev", "requirements-build"):
        assert backend in (ROOT / f"{name}.in").read_text("utf-8").split()
        assert f"{backend} \\" in (ROOT / f"{name}.txt").read_text("utf-8")
