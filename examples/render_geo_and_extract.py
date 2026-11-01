# SPDX-License-Identifier: Apache-2.0
"""JS rendering + geo targeting + CSS extraction (needs sdo-sdk[parse])."""

from sdo import Client, ScrapeConfig

with Client() as client:  # SDO_TOKEN
    cfg = ScrapeConfig(
        url="https://example.com",
        render=True,  # JS rendering
        geo_code="us",  # route from the US
        wait_until="networkidle0",
        block_resources=True,
    )
    result = client.scrape(cfg)
    print("status:", result.status_code)

    # CSS / XPath extraction via parsel (the raw HTML output, not markdown)
    heading = result.selector().css("h1::text").get()
    print("h1:", heading)

    # output="markdown" returns the page as markdown text instead of HTML
    markdown = client.scrape("https://example.com", output="markdown").text
    print(markdown[:200])
