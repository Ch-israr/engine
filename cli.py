"""CLI for the TwitterDown engine: print a tweet's videos as JSON.

Usage:
    python cli.py <tweet-url> [--route auto|app|graphql|syndication]

Exit codes: 0 ok, 1 usage error, 2 extraction failed (error printed as JSON).
"""

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from engine import EngineError, extract


def main(argv):
    route = "auto"
    urls = []
    i = 0
    while i < len(argv):
        if argv[i] == "--route" and i + 1 < len(argv):
            route = argv[i + 1]
            i += 2
        else:
            urls.append(argv[i])
            i += 1
    if len(urls) != 1 or route not in ("auto", "app", "graphql", "syndication"):
        print(__doc__.strip(), file=sys.stderr)
        return 1
    try:
        result = extract(urls[0], route=route)
    except EngineError as e:
        print(json.dumps({"error": e.message, "http_status": e.http_status}, indent=2))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
