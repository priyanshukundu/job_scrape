"""Lists boards that look dead or frozen: zero postings, or an unchanged posting set for N days."""
import json
import sys
from datetime import date
from pathlib import Path

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
rec = json.loads((Path(__file__).parent / "data" / "board_freshness.json").read_text(encoding="utf-8"))
today = date.today()
rows = [(name, r["n"], (today - date.fromisoformat(r["changed"])).days) for name, r in rec.items()]
print(f"{len(rec)} boards tracked")
print("EMPTY:", ", ".join(n for n, c, _ in rows if c == 0) or "none")
for name, count, days in sorted((x for x in rows if x[1] and x[2] >= DAYS), key=lambda x: -x[2]):
    print(f"FROZEN {days}d: {name} ({count} postings)")
