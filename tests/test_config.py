# SPDX-License-Identifier: Apache-2.0

import pytest

from sdo import ScrapeConfig
from sdo.errors import SdoConfigError

U = "https://e.com"


def params_dict(cfg: ScrapeConfig) -> dict:
    """Collapse to a dict for single-valued assertions (loses repeats)."""
    return dict(cfg.to_params())


def test_url_is_required():
    with pytest.raises(SdoConfigError):
        ScrapeConfig(url="")
    with pytest.raises(SdoConfigError):
        ScrapeConfig(url=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1",
        "http://[::1]x/",
        "https://e.com:",
        "https://e.com:1:2",
        "https://e.com:080000",
        "https://e.com/\u00a0",
        "https://e.com/\u0085",
        "https://e.com/\\",
        "https://@e.com:0",
        "http\u017f://e.com/",  # long s: case-folds to "s" without re.ASCII
        "\u212aTTP://e.com/",  # Kelvin sign
    ],
)
def test_malformed_target_url_is_a_config_error(url):
    with pytest.raises(SdoConfigError):
        ScrapeConfig(url=url)


@pytest.mark.parametrize(
    "url", ["https://[::1]/", "https://a@b@e.com/", "https://e.com:65535", "HTTP://E.COM?x#y"]
)
def test_well_formed_target_url_is_sent_as_given(url):
    assert params_dict(ScrapeConfig(url=url))["url"] == url


def test_fields_after_url_are_keyword_only():
    """So a new field can be added in spec order without breaking callers."""
    with pytest.raises(TypeError):
        ScrapeConfig(U, True)  # type: ignore[misc]
    assert ScrapeConfig(U, render=True).render is True


def test_target_url_rechecked_at_serialization():
    cfg = ScrapeConfig(url=U)
    cfg.url = "file:///etc/passwd"
    with pytest.raises(SdoConfigError):
        cfg.to_params()


def test_minimal_serialization():
    assert params_dict(ScrapeConfig(url=U)) == {"url": U}


def test_true_bools_serialize_as_lowercase_strings():
    assert params_dict(ScrapeConfig(url=U, super_proxy=True))["super"] == "true"


def test_false_bools_are_omitted():
    """A false boolean is absent from the wire, never `key=false`."""
    assert params_dict(ScrapeConfig(url=U, super_proxy=False, pure_cookies=False)) == {"url": U}


def test_camelcase_wire_names():
    p = params_dict(
        ScrapeConfig(
            url=U,
            geo_code="us",
            return_json=True,
            disable_redirection=True,
            set_cookies="a=b",
            session_id="1234",
        )
    )
    assert p["geoCode"] == "us"
    assert p["returnJSON"] == "true"
    assert p["disableRedirection"] == "true"
    assert p["setCookies"] == "a=b"
    assert p["sessionId"] == "1234"


def test_render_only_param_without_render_raises():
    with pytest.raises(SdoConfigError, match="requires render"):
        ScrapeConfig(url=U, screenshot=True).to_params()


def test_render_only_param_with_render_ok():
    p = params_dict(ScrapeConfig(url=U, render=True, return_json=True, screenshot=True))
    assert p["render"] == "true"
    assert p["screenShot"] == "true"


def test_render_new_is_rejected():
    """render takes only the documented true and false."""
    with pytest.raises(SdoConfigError, match="render"):
        ScrapeConfig(url=U, render="new").to_params()


def test_render_false_string_is_omitted():
    """render="false" disables rendering, so it is dropped like a false bool."""
    assert params_dict(ScrapeConfig(url=U, render="False")) == {"url": U}


@pytest.mark.parametrize("render", [1, 2.5, ["true"]])
def test_render_rejects_non_bool_non_str(render):
    with pytest.raises(SdoConfigError, match="render"):
        ScrapeConfig(url=U, render=render).to_params()


def test_play_with_browser_is_canonical_json():
    p = params_dict(
        ScrapeConfig(url=U, render=True, play_with_browser=[{"css": "#x", "action": "click"}])
    )
    assert p["playWithBrowser"] == '[{"action":"click","css":"#x"}]'


def test_json_params_reject_wrong_shapes_and_unserializable_values():
    with pytest.raises(SdoConfigError, match="play_with_browser must be a list"):
        ScrapeConfig(url=U, render=True, play_with_browser={"a": 1}).to_params()  # type: ignore[arg-type]
    for action in ({"k": object()}, {"k": float("nan")}, {1: "int key"}):
        with pytest.raises(SdoConfigError, match="not JSON-serializable"):
            ScrapeConfig(url=U, render=True, play_with_browser=[action]).to_params()


@pytest.mark.parametrize(
    ("field", "value"),
    [("device", "watch"), ("output", "pdf"), ("device", 3)],
)
def test_invalid_enum_rejected(field, value):
    with pytest.raises(SdoConfigError, match=field):
        ScrapeConfig(url=U, **{field: value}).to_params()


def test_invalid_wait_until_rejected():
    with pytest.raises(SdoConfigError):
        ScrapeConfig(url=U, render=True, wait_until="whenever").to_params()


def test_enums_are_sent_lowercase():
    assert params_dict(ScrapeConfig(url=U, device="Mobile"))["device"] == "mobile"


@pytest.mark.parametrize(("field", "value"), [("super_proxy", "true"), ("super_proxy", 1)])
def test_bool_fields_take_bools(field, value):
    with pytest.raises(SdoConfigError, match="must be a bool"):
        ScrapeConfig(url=U, **{field: value}).to_params()


def test_string_fields_take_strings():
    with pytest.raises(SdoConfigError, match="geo_code must be a str"):
        ScrapeConfig(url=U, geo_code=5).to_params()  # type: ignore[arg-type]


def test_timeout_is_milliseconds_and_range_checked():
    """scrape(url, timeout=30) reads like seconds; it would be a 30 ms gateway
    budget, so it is rejected with a message naming the unit."""
    with pytest.raises(SdoConfigError, match="milliseconds") as ei:
        ScrapeConfig(url=U, timeout=30).to_params()
    assert "Client(timeout=...)" in str(ei.value)
    with pytest.raises(SdoConfigError, match="milliseconds"):
        ScrapeConfig(url=U, timeout=0.5).to_params()  # type: ignore[arg-type]
    with pytest.raises(SdoConfigError, match="milliseconds") as ei:
        ScrapeConfig(url=U, retry_timeout=60000).to_params()
    assert "Client(timeout=...)" not in str(ei.value)
    assert params_dict(ScrapeConfig(url=U, timeout=30000))["timeout"] == "30000"


def test_int_fields_reject_bools_and_floats():
    with pytest.raises(SdoConfigError, match="height must be an integer"):
        ScrapeConfig(url=U, render=True, height=True).to_params()  # type: ignore[arg-type]
    with pytest.raises(SdoConfigError, match="width must be an integer"):
        ScrapeConfig(url=U, render=True, width=10.5).to_params()  # type: ignore[arg-type]


def test_extra_params_escape_hatch():
    p = params_dict(
        ScrapeConfig(
            url=U,
            extra_params={"brandNewParam": "x", "flag": True, "off": False, "n": 1.0, "skip": None},
        )
    )
    assert p == {"url": U, "brandNewParam": "x", "flag": "true", "off": "false", "n": "1"}


@pytest.mark.parametrize(
    "extra",
    [
        {"k": [1]},
        {"k": float("inf")},
        {1: "x"},
        {"": "x"},
        ["not", "a", "dict"],
    ],
)
def test_extra_params_rejects_bad_keys_and_values(extra):
    with pytest.raises(SdoConfigError, match="extra_params"):
        ScrapeConfig(url=U, extra_params=extra).to_params()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    ["a b", "a&token=x", "a=b", "k[]", "1st", "_x", "-x", ".x", "tok\u0435n", "x\n", "\u017fkip"],
)
def test_extra_params_key_must_be_a_parameter_name(key):
    with pytest.raises(SdoConfigError, match="not a valid parameter name"):
        ScrapeConfig(url=U, extra_params={key: "x"}).to_params()


def test_extra_params_reserved_keys_keep_their_message():
    with pytest.raises(SdoConfigError, match="may not set 'Token'"):
        ScrapeConfig(url=U, extra_params={"Token": "x"}).to_params()


def test_extra_params_key_shapes_the_api_uses_are_accepted():
    keys = ["a", "Z9", "include_airlines", "geo.code", "x-y", "camelCase_1.2-3"]
    p = params_dict(ScrapeConfig(url=U, extra_params={k: "1" for k in keys}))
    assert all(p[k] == "1" for k in keys)


def test_validate_raises_like_to_params():
    ScrapeConfig(url=U).validate()
    with pytest.raises(SdoConfigError):
        ScrapeConfig(url=U, device="phone").validate()


# block_resources is tri-state: None sends nothing (the gateway default, True,
# applies), True is render-only, and False is sent so resources load.
def test_block_resources_false_is_sent_and_needs_no_render():
    assert (
        params_dict(ScrapeConfig(url=U, render=True, block_resources=False))["blockResources"]
        == "false"
    )
    assert ScrapeConfig(url=U, block_resources=False).to_params() == [
        ("url", U),
        ("blockResources", "false"),
    ]
    assert "blockResources" not in params_dict(ScrapeConfig(url=U, render=True))
    with pytest.raises(SdoConfigError):
        ScrapeConfig(url=U, block_resources=True).to_params()
    for bad in (0, 1, "false"):
        with pytest.raises(SdoConfigError):
            ScrapeConfig(url=U, render=True, block_resources=bad).to_params()  # type: ignore[arg-type]
