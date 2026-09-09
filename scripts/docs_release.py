"""Select the highest published final version from paginated GitHub releases."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


def latest_release(pages: list[list[dict[str, Any]]]) -> str:
    versions: dict[tuple[int, ...], str] = {}
    for page in pages:
        for release in page:
            tag = release["tag_name"]
            if release["draft"] or release["prerelease"] or not release["published_at"]:
                continue
            if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
                continue
            versions[tuple(map(int, tag[1:].split(".")))] = tag
    return versions[max(versions)] if versions else ""


if __name__ == "__main__":
    print(latest_release(json.load(sys.stdin)))
