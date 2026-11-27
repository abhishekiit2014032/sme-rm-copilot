"""Export the demo UI as one self-contained static page: demo/dist/index.html.

Runs every scenario through demo.scenarios.run_all() and embeds the recorded runs (answers and
traces) as JSON in the page, so it can be opened from disk or any static host with no server and
no network calls. If the scenarios cannot be imported or run, it falls back to the sample data in
demo/fixtures/sample_runs.json and says so.

It also refreshes the same embedded block in demo/ui/index.html (the page the FastAPI app serves),
so the live page falls back to the same recording when /ask is unreachable. Pass --no-ui to skip.

Usage: python demo/export_static.py [--fixture] [--no-ui] [--out PATH]
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import enum
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "demo" / "ui" / "index.html"
FIXTURE = ROOT / "demo" / "fixtures" / "sample_runs.json"
DIST = ROOT / "demo" / "dist" / "index.html"
BLOCK = re.compile(r'(<script id="recorded-runs" type="application/json">)(.*?)(</script>)', re.S)


def _jsonable(obj: Any) -> Any:
    """json.dumps default: pydantic models, dataclasses, enums, sets and dates."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def load_runs(force_fixture: bool = False) -> tuple[list[dict], str]:
    """Return (runs, source) where source is "scenarios" or "fixture"."""
    if not force_fixture:
        for p in (ROOT / "src", ROOT):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        try:
            from demo.scenarios import run_all  # noqa: PLC0415

            runs = json.loads(json.dumps(run_all(), default=_jsonable))
            if not runs:
                raise ValueError("run_all() returned no runs")
            return runs, "scenarios"
        except Exception:  # noqa: BLE001  any failure falls back to the recorded fixture
            print("demo.scenarios.run_all() failed; using demo/fixtures/sample_runs.json", file=sys.stderr)
            traceback.print_exc(limit=3)
    return json.loads(FIXTURE.read_text(encoding="utf-8")), "fixture"


def embed(html: str, payload: dict) -> str:
    # "</" is escaped so no string inside the data can close the script element early.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if not BLOCK.search(html):
        raise SystemExit("demo/ui/index.html has no recorded-runs block")
    return BLOCK.sub(lambda m: m.group(1) + data + m.group(3), html, count=1)


def check_self_contained(html: str) -> None:
    """The static page must not load anything: no external scripts, styles, fonts or images."""
    bad = re.findall(r'<(?:script|link|img|iframe)[^>]+(?:src|href)=["\'](?:https?:)?//', html, re.I)
    bad += re.findall(r'url\(["\']?(?:https?:)?//', html, re.I)
    bad += re.findall(r"@import", html)
    if bad:
        raise SystemExit(f"static page would load external resources: {bad[:3]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", action="store_true", help="use demo/fixtures/sample_runs.json, do not run scenarios")
    ap.add_argument("--no-ui", action="store_true", help="do not refresh the embedded runs in demo/ui/index.html")
    ap.add_argument("--out", type=Path, default=DIST)
    args = ap.parse_args()

    runs, source = load_runs(args.fixture)
    payload = {
        "source": source,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "runs": runs,
    }
    template = UI.read_text(encoding="utf-8")

    page = embed(template, payload).replace("<html lang=\"en\">", "<html lang=\"en\" data-mode=\"static\">", 1)
    page = page.replace("<title>SME RM Copilot demo</title>", "<title>SME RM Copilot recorded demo</title>", 1)
    check_self_contained(page)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")

    if not args.no_ui:
        UI.write_text(embed(template, payload), encoding="utf-8")

    steps = sum(len(r.get("steps", [])) for r in runs)
    spans = sum(len((s.get("trace") or (s.get("answer") or {}).get("trace") or {}).get("spans", []))
                for r in runs for s in r.get("steps", []))
    print(f"wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}: "
          f"{len(runs)} scenarios, {steps} steps, {spans} spans, data from {source}, {len(page) // 1024} KB")


if __name__ == "__main__":
    main()
