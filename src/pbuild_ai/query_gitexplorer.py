#!/usr/bin/env python3
"""Wrapper around gitexplorer.opensuse.org API.
Queries by file or package name, returns deduplicated (package_name, tracking_branch) pairs.
"""

import json
import ssl
import urllib.request
import urllib.parse
import sys

API_BASE = "https://gitexplorer.opensuse.org/api/products"
_CTX = ssl.create_default_context()
# gitexplorer uses a Let's Encrypt cert that may chain differently in some envs
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _deduplicate(raw_list, limit):
    seen = set()
    results = []
    for item in raw_list:
        pkgs = item.get("packages", [item])  # files endpoint nests packages, packages endpoint has flat items
        for p in pkgs:
            pair = (p["package_name"], p.get("tracking_branch", "") or "")
            if pair not in seen:
                seen.add(pair)
                results.append({
                    "package_name": p["package_name"],
                    "tracking_branch": p.get("tracking_branch", "") or "",
                })
                if len(results) >= limit:
                    return results
    return results


def query_package_by_file(filename: str, limit: int = 20) -> list[dict]:
    """Find packages providing a file. Returns deduplicated [{package_name, tracking_branch}]."""
    url = f"{API_BASE}/files?q={urllib.parse.quote(filename)}"
    req = urllib.request.Request(url, headers={"User-Agent": "pbuild-ai/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _deduplicate(data.get("results", []), limit=limit)


def query_package_by_name(search_term: str, limit: int = 20) -> list[dict]:
    """Find packages matching a name substring (case-insensitive). Returns deduplicated [{package_name, tracking_branch}]."""
    url = f"{API_BASE}/packages?q={urllib.parse.quote(search_term)}"
    req = urllib.request.Request(url, headers={"User-Agent": "pbuild-ai/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _deduplicate(data.get("packages", []), limit=limit)


def format_results(results: list[dict]) -> str:
    """Format deduplicated results as compact lines:  Mesa-devel (slfo-1.2)"""
    lines = []
    for r in sorted(results, key=lambda x: (x["package_name"], x["tracking_branch"])):
        suffix = f" ({r['tracking_branch']})" if r.get("tracking_branch") else ""
        lines.append(f"  {r['package_name']}{suffix}")
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Query gitexplorer for package info")
    parser.add_argument("--type", choices=["files", "packages"], default="files",
                        help="'files' to find packages providing a file, 'packages' to search by name")
    parser.add_argument("query", help="Filename or package name to search for")
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    args = parser.parse_args()

    if args.type == "files":
        results = query_package_by_file(args.query, limit=args.limit)
    else:
        results = query_package_by_name(args.query, limit=args.limit)

    if not results:
        sys.exit(1)
    print(format_results(results))


if __name__ == "__main__":
    main()
