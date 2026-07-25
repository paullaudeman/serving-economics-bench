#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "matplotlib>=3.8",
# ]
# ///
"""Aggregate results.jsonl into results.md (per-cell table) and chart.png
(latency-vs-throughput scatter with cost-per-completed-task annotations).

Pricing comes exclusively from pricing.json. Bedrock costs are reported as
"n/a" while pricing.json still carries PLACEHOLDER (-1.0) values - fill them
before quoting any Bedrock cost number.

Chart method (dataviz skill): single axis system (no dual axes), categorical
color by provider in fixed order (anthropic=blue, bedrock=orange - palette
validated with the skill's validator, all checks PASS on the light surface),
direct labels on every point plus a legend, text in ink colors (never series
colors), recessive hairline grid.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parent
RESULTS_PATH = ROOT / "results.jsonl"
PRICING_PATH = ROOT / "pricing.json"
OUT_MD = ROOT / "results.md"
OUT_PNG = ROOT / "chart.png"

# Validated palette (dataviz reference palette, light mode; validator: PASS).
SURFACE = "#fcfcfb"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
SERIES = {"anthropic": "#2a78d6", "bedrock": "#eb6834"}  # fixed order, never cycled
PROVIDER_ORDER = ["anthropic", "bedrock"]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def load_rows() -> list[dict]:
    if not RESULTS_PATH.exists():
        sys.exit(f"{RESULTS_PATH} not found - run bench.py first.")
    rows = []
    for line in RESULTS_PATH.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        sys.exit("results.jsonl is empty - run bench.py first.")
    return rows


def cell_cost_usd(rows: list[dict], pricing: dict) -> float | None:
    p = pricing[rows[0]["provider"]]["models"][rows[0]["model"]]
    if min(p["input_per_mtok"], p["output_per_mtok"],
           p["cache_write_per_mtok"], p["cache_read_per_mtok"]) < 0:
        return None  # PLACEHOLDER pricing - refuse to fabricate a number
    total = 0.0
    for r in rows:
        total += (
            r["input_tokens"] * p["input_per_mtok"]
            + r["output_tokens"] * p["output_per_mtok"]
            + r["cache_write_tokens"] * p["cache_write_per_mtok"]
            + r["cache_read_tokens"] * p["cache_read_per_mtok"]
        ) / 1e6
    return total


def aggregate(rows: list[dict], pricing: dict) -> list[dict]:
    by_cell: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cell[r["cell_id"]].append(r)
    cells = []
    for cell_id, cell_rows in sorted(by_cell.items()):
        r0 = cell_rows[0]
        ttfts = [r["ttft_s"] for r in cell_rows if r["ttft_s"] is not None]
        itl50 = [r["itl_p50_s"] for r in cell_rows if r["itl_p50_s"] is not None]
        itl99 = [r["itl_p99_s"] for r in cell_rows if r["itl_p99_s"] is not None]
        walls = [r["wall_s"] for r in cell_rows]
        out_tokens = sum(r["output_tokens"] for r in cell_rows)
        # Throughput: output tokens per second of generation time (wall - ttft).
        gen_time = sum(max(1e-6, r["wall_s"] - (r["ttft_s"] or 0)) for r in cell_rows)
        passed = sum(1 for r in cell_rows if r["passed"])
        total_wall = sum(walls)
        cost = cell_cost_usd(cell_rows, pricing)
        cells.append({
            "cell_id": cell_id,
            "provider": r0["provider"],
            "model": r0["model"],
            "context": r0["context"],
            "cache": r0["cache"],
            "concurrency": r0["concurrency"],
            "n": len(cell_rows),
            "passed": passed,
            "pass_rate": passed / len(cell_rows),
            "retries": sum(r["retries"] for r in cell_rows),
            "throttle_retries": sum(r.get("throttle_retries", 0) for r in cell_rows),
            "ttft_p50_s": pct(ttfts, 0.50),
            "itl_p50_s": statistics.median(itl50) if itl50 else None,
            "itl_p99_s": pct(itl99, 0.99),
            "wall_p50_s": pct(walls, 0.50),
            "tokens_per_s": out_tokens / gen_time if gen_time else None,
            "cost_usd": cost,
            "cost_per_completed_task": (cost / passed) if (cost is not None and passed) else None,
            # Goodput: schema-valid completed tasks per minute of wall time.
            "goodput_tasks_per_min": passed / (total_wall / 60) if total_wall else None,
            "mock": any(r.get("mock") for r in cell_rows),
        })
    return cells


def fmt(v, spec="{:.3f}", na="n/a") -> str:
    return na if v is None else spec.format(v)


def write_markdown(cells: list[dict], any_mock: bool, bedrock_placeholder: bool) -> None:
    lines = ["# serving-economics-bench - results", ""]
    if any_mock:
        lines += ["> **DRY RUN (mock data).** These numbers were produced by the "
                  "mock client to validate the harness. They are NOT provider "
                  "measurements - never quote them.", ""]
    if bedrock_placeholder:
        lines += ["> **Bedrock costs are n/a**: pricing.json still carries "
                  "PLACEHOLDER values. Fill them from the AWS Bedrock pricing "
                  "page before quoting any Bedrock cost.", ""]
    lines += [
        "| cell | n | pass | retries | TTFT p50 (s) | ITL p50 (s) | ITL p99 (s) "
        "| wall p50 (s) | tok/s | cost ($) | $/completed task | goodput (tasks/min) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cells:
        lines.append(
            f"| {c['cell_id']} | {c['n']} | {c['passed']}/{c['n']} | {c['retries']} "
            f"| {fmt(c['ttft_p50_s'])} | {fmt(c['itl_p50_s'], '{:.4f}')} "
            f"| {fmt(c['itl_p99_s'], '{:.4f}')} | {fmt(c['wall_p50_s'], '{:.2f}')} "
            f"| {fmt(c['tokens_per_s'], '{:.1f}')} | {fmt(c['cost_usd'], '{:.4f}')} "
            f"| {fmt(c['cost_per_completed_task'], '{:.5f}')} "
            f"| {fmt(c['goodput_tasks_per_min'], '{:.2f}')} |"
        )
    lines += [
        "",
        "Definitions: TTFT = time to first streamed text token. ITL = "
        "inter-token latency within a request (p50/p99 of deltas), cell value "
        "is the median (p50) / p99 across requests. tok/s = output tokens per "
        "second of generation time (wall - TTFT). A completed task = "
        "schema-valid JSON matching the expected output; retried attempts' "
        "tokens are included in cost. Goodput = completed tasks per minute of "
        "total wall time.",
        "",
    ]
    OUT_MD.write_text("\n".join(lines))


def label_for(c: dict) -> str:
    bits = [c["model"], c["context"]]
    if c["cache"]:
        bits.append("cache")
    if c["concurrency"] > 1:
        bits.append(f"c{c['concurrency']}")
    return "·".join(bits)


def write_chart(cells: list[dict], any_mock: bool) -> None:
    fig, ax = plt.subplots(figsize=(9, 6), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    plotted: list[tuple[dict, float, float]] = []
    for provider in PROVIDER_ORDER:  # fixed assignment order, never cycled
        pts = [c for c in cells if c["provider"] == provider
               and c["ttft_p50_s"] is not None and c["tokens_per_s"] is not None]
        if not pts:
            continue
        xs = [c["ttft_p50_s"] for c in pts]
        ys = [c["tokens_per_s"] for c in pts]
        ax.scatter(xs, ys, s=90, color=SERIES[provider], label=provider,
                   edgecolors=SURFACE, linewidths=1.5, zorder=3)
        plotted.extend(zip(pts, xs, ys))

    # Direct labels with greedy de-collision: near-coincident points get
    # staggered vertical offsets; points near the right edge label leftward.
    if plotted:
        all_x = [x for _, x, _ in plotted]
        all_y = [y for _, _, y in plotted]
        xr = (max(all_x) - min(all_x)) or 1.0
        yr = (max(all_y) - min(all_y)) or 1.0
        ax.margins(x=0.12, y=0.06)  # headroom so edge labels stay inside
        placed: list[tuple[float, float]] = []
        stagger = [(7, 5), (7, -13), (7, 16), (7, -24), (7, 27)]
        for c, x, y in sorted(plotted, key=lambda t: (t[1], -t[2])):
            cost = c["cost_per_completed_task"]
            cost_txt = f"  ${cost:,.4f}/task" if cost is not None else "  $ n/a"
            # "Near" accounts for label width: a rendered label spans roughly
            # a quarter of the x range at this figure size, so any placed
            # point within that band and about one text row vertically forces this
            # label onto the next stagger slot.
            near = sum(1 for px, py in placed
                       if abs(px - x) < 0.25 * xr and abs(py - y) < 0.055 * yr)
            dx, dy = stagger[min(near, len(stagger) - 1)]
            right_edge = x > max(all_x) - 0.18 * xr
            ax.annotate(label_for(c) + cost_txt, (x, y),
                        textcoords="offset points",
                        xytext=(-dx, dy) if right_edge else (dx, dy),
                        ha="right" if right_edge else "left",
                        fontsize=7.5, color=INK_2, zorder=4)
            placed.append((x, y))

    ax.set_xlabel("TTFT p50 (seconds) - lower is better", color=INK_2, fontsize=10)
    ax.set_ylabel("Output tokens / second - higher is better", color=INK_2, fontsize=10)
    title = "Serving economics: latency vs throughput, cost per completed task"
    if any_mock:
        title += "  [DRY RUN - MOCK DATA]"
    ax.set_title(title, color=INK, fontsize=12, loc="left", pad=14)

    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    for spine in ["left", "bottom"]:
        ax.spines[spine].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=8.5)

    leg = ax.legend(loc="upper right", frameon=False, fontsize=9)
    for text in leg.get_texts():
        text.set_color(INK_2)

    fig.tight_layout()
    fig.savefig(OUT_PNG, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    # Optional single argument: a results directory (e.g. results-ec2/<ts>/
    # from `make pull`). Reads results.jsonl there and writes results.md and
    # chart.png next to it, leaving the repo-root outputs untouched.
    global RESULTS_PATH, OUT_MD, OUT_PNG
    if len(sys.argv) > 1:
        d = pathlib.Path(sys.argv[1]).resolve()
        RESULTS_PATH = d / "results.jsonl"
        OUT_MD = d / "results.md"
        OUT_PNG = d / "chart.png"
    rows = load_rows()
    pricing = json.loads(PRICING_PATH.read_text())
    any_mock = any(r.get("mock") for r in rows)
    bedrock_placeholder = min(
        v for m in pricing["bedrock"]["models"].values()
        for k, v in m.items() if k.endswith("_per_mtok")
    ) < 0
    cells = aggregate(rows, pricing)
    write_markdown(cells, any_mock, bedrock_placeholder)
    write_chart(cells, any_mock)
    print(f"Wrote {OUT_MD.name} ({len(cells)} cells) and {OUT_PNG.name}")
    if any_mock:
        print("NOTE: results contain DRY-RUN mock rows - outputs are labeled accordingly.")
    if bedrock_placeholder:
        print("NOTE: Bedrock pricing is PLACEHOLDER - Bedrock costs reported as n/a.")


if __name__ == "__main__":
    main()
