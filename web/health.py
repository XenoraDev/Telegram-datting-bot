"""Docker HEALTHCHECK for the web service: GET /healthz on localhost with the real Host header.

``python -m web.health`` exits 0 when the app answers 200 (which includes a database ping).
"""

from __future__ import annotations

import os
import sys
import urllib.request


def probe(port: int, host: str, timeout: float = 4.0) -> bool:
    request = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 URL
        f"http://127.0.0.1:{port}/healthz", headers={"Host": host}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status == 200
    except OSError:
        return False


def main() -> int:
    host = os.environ.get("ADMIN_DOMAIN", "").strip().lower()
    port = int(os.environ.get("WEB_PORT", "8000"))
    return 0 if host and probe(port, host) else 1


if __name__ == "__main__":
    sys.exit(main())
