#!/usr/bin/env python3
"""
Status line for plan sessions: shows model and cost, and keeps the cost where run_plan.py reads it.

    statusline.py <session dir>

Claude Code pipes the session's state in as JSON on every refresh, including
cost.total_cost_usd for this Claude process. It is written to <session dir>/cost.json under the
process's session_id, so a `claude --continue` after a crash (which starts its own count) adds
to the window's total instead of replacing it.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    cost = float((event.get("cost") or {}).get("total_cost_usd") or 0)
    model = (event.get("model") or {}).get("display_name") or (event.get("model") or {}).get("id") or "?"
    sid = event.get("session_id") or "unknown"
    total = cost
    if out_dir:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            f = out_dir / "cost.json"
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                data = {}
            by = data.setdefault("by_session", {})
            by[sid] = {"usd": cost, "model": model, "updated": datetime.now().isoformat(timespec="seconds")}
            tmp = f.with_name(f"cost.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(f)
            total = sum(float(v.get("usd", 0)) for v in by.values())
        except OSError:
            pass  # a status line must never break the session
    money = lambda x: "<$0.01" if 0 < x < 0.005 else f"${x:,.2f}"
    print(f"{model} · {money(total)}" + (f" (this run {money(cost)})" if abs(total - cost) > 0.005 else ""))


if __name__ == "__main__":
    main()
