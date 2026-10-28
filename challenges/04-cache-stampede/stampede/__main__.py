"""``uv run stampede``: the page, the simulator and the real Comments API on
http://127.0.0.1:8091/."""

from __future__ import annotations

import argparse
import webbrowser
from collections.abc import Sequence

from stampede.api import serve


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stampede")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    server = serve(args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"stampede on {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
