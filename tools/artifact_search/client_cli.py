"""Read-only command-line and JSON tool entrypoints for Mímir artifact search."""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata

from .agent_tool import TOOL_DEFINITION, handle_tool_input
from .client import ClientError, search


_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid arguments")


def _error(status: str, code: str) -> dict:
    safe_code = code if isinstance(code, str) and _CODE.fullmatch(code) else "client_error"
    return {"schema": "mimir-artifact-search-v1", "status": status, "error": {"code": safe_code}}


def _safe_text(value) -> str:
    """Collapse controls and line breaks from tainted indexed text for terminals."""
    chars = []
    for char in str(value):
        category = unicodedata.category(char)
        chars.append(" " if category.startswith("C") or char in "\r\n\t" else char)
    return " ".join("".join(chars).split())


def _human(receipt: dict) -> str:
    results = receipt.get("results", [])
    if not results:
        return "Inga träffar. Det bevisar inte att informationen saknas."
    lines = []
    for result in results:
        path = _safe_text(result.get("path", ""))
        locator = result.get("locator") or {}
        kind = _safe_text(locator.get("kind", "källa"))
        number = locator.get("number")
        location = f"{kind} {number}" if number is not None else kind
        snippet = _safe_text(result.get("snippet", ""))
        url = _safe_text(result.get("mimir_url", ""))
        lines.append(f"{path} ({location})\n  {snippet}\n  {url}")
    return "\n".join(lines)


def _parse_tool_json(raw: str):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None
    return value


def _stdin_bytes(limit: int) -> bytes | None:
    """Read a strict byte bound from real stdin, with a text-stream test fallback."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        return stream.read(limit + 1)
    text = sys.stdin.read(limit + 1)
    try:
        return text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("query", nargs="?", help="Search text (omit when using --query-stdin or --tool)")
    parser.add_argument("--top", help="Number of results (1–5; default 5)")
    parser.add_argument("--config", help="Path to the client profile")
    parser.add_argument("--json", action="store_true", help="Emit the validated receipt as JSON")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--query-stdin", action="store_true", help="Read plain search text from stdin")
    modes.add_argument("--tool", action="store_true", help="Read one JSON tool object from stdin and emit one JSON envelope")
    modes.add_argument("--tool-schema", action="store_true", help="Print the tool definition without contacting Mímir")
    try:
        args = parser.parse_args(argv)
    except ValueError:
        print(json.dumps(_error("invalid_request", "invalid_request")))
        return 2

    if args.tool_schema:
        if args.query is not None or args.top is not None or args.config is not None or args.json or args.query_stdin:
            print(json.dumps(_error("invalid_request", "invalid_request")))
            return 2
        print(json.dumps(TOOL_DEFINITION, ensure_ascii=False))
        return 0

    if args.tool:
        if args.query is not None or args.top is not None or args.json:
            print(json.dumps(_error("invalid_request", "invalid_request")))
            return 2
        raw_bytes = _stdin_bytes(8192)
        try:
            raw = raw_bytes.decode("utf-8", errors="strict") if raw_bytes is not None and len(raw_bytes) <= 8192 else None
        except UnicodeDecodeError:
            raw = None
        request = _parse_tool_json(raw) if raw is not None else None
        response = handle_tool_input(request, profile_path=args.config)
        print(json.dumps(response, ensure_ascii=False))
        if response["status"] == "invalid_request":
            return 2
        if response["status"] == "unavailable":
            return 1
        return 0

    if args.query_stdin and args.query is not None or not args.query_stdin and args.query is None:
        print(json.dumps(_error("invalid_request", "invalid_request")))
        return 2
    try:
        top = 5 if args.top is None else int(args.top)
    except ValueError:
        top = 0
    if isinstance(top, bool) or not 1 <= top <= 5:
        print(json.dumps(_error("invalid_request", "invalid_top")))
        return 2
    if args.query_stdin:
        raw_bytes = _stdin_bytes(16386)
        try:
            query = raw_bytes.decode("utf-8", errors="strict") if raw_bytes is not None and len(raw_bytes) <= 16386 else None
        except UnicodeDecodeError:
            query = None
        if query is None or len(query) > 4096:
            print(json.dumps(_error("invalid_request", "invalid_query")))
            return 2
        query = query.rstrip("\r\n")
    else:
        query = args.query
    if not query.strip() or len(query) > 4096:
        print(json.dumps(_error("invalid_request", "invalid_query")))
        return 2

    try:
        receipt = search(query, top, profile_path=args.config)
    except ClientError as error:
        code = error.code if isinstance(error.code, str) else "client_error"
        status = "invalid_request" if code == "invalid_request" else "unavailable"
        print(json.dumps(_error(status, code)))
        return 2 if status == "invalid_request" else 1
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        print(_human(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
