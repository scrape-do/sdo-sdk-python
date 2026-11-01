# SPDX-License-Identifier: Apache-2.0
"""Single source of truth for the installed SDK version.

It appears in exactly one request header, the User-Agent
(``sdo-sdk-python/<version>``); the SDK sends no other client-identifying data.
"""

__version__ = "0.1.0"
