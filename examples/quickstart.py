# SPDX-License-Identifier: Apache-2.0
"""Minimal quickstart. Set SDO_TOKEN in your environment first.

export SDO_TOKEN=your_token
python examples/quickstart.py
"""

from sdo import Client

with Client() as client:  # reads SDO_TOKEN
    result = client.scrape("https://httpbin.org/anything")
    print("status:", result.status_code)
    print("cost:", result.cost)
    print("body:", result.text[:200])
