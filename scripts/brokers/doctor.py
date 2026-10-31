#!/usr/bin/env python3
"""Read-only broker gateway doctor; never places a test order."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.trading.brokers.runtime import report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", choices=("futu", "ibkr", "qmt"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    data = json.loads(args.config.read_text(encoding="utf-8"))
    result = report(
        broker=args.broker,
        gateway_id=str(data.get("gateway_id") or f"{args.broker}-local"),
        host=str(data.get("host") or "127.0.0.1"),
        port=int(data.get("port") or (11111 if args.broker == "futu" else 4002)),
        environment=str(data.get("environment") or "PAPER"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.as_json else _text(result))
    return 0 if result["read_capability"] == "READY" else 3


def _text(result: dict) -> str:
    lines = [f"{result['broker']} {result['gateway_id']} · {result['read_capability']}"]
    lines.extend(f"[{item['status']}] {item['id']}: {item['detail']}" for item in result["checks"])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
