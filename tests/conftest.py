# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures. The suite is hermetic: nothing in the caller's environment
(a sandbox ``SDO_BASE_URL``, a real ``SDO_TOKEN``) may change what a test sees."""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_sdo_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("SDO_"):
            monkeypatch.delenv(name)
