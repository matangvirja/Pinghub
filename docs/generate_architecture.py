"""Render docs/architecture.png with matplotlib.

Run from the repository root:
    python docs/generate_architecture.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent / "architecture.png"

COLORS = {
    "client": "#e8f0fe",
    "gateway": "#dbeafe",
    "service": "#dcfce7",
    "queue": "#fef3c7",
    "data": "#fce7f3",
    "worker": "#ede9fe",
    "edge": "#334155",
    "text": "#0f172a",
}


def box(ax, x, y, w, h, title, subtitle="", color="#ffffff", title_size=13):
    patch = FancyBboxPatch(
        (x - w / 2, y - h / 2),
        w,
        h,
        boxstyle="round,pad=0.02,rounding_size=0.12",
        linewidth=1.6,
        edgecolor=COLORS["edge"],
        facecolor=color,
        zorder=2,
    )
    ax.add_patch(patch)
    ax.text(
        x,
        y + (0.09 if subtitle else 0),
        title,
        ha="center",
        va="center",
        fontsize=title_size,
        fontweight="bold",
        color=COLORS["text"],
        zorder=3,
    )
    if subtitle:
        ax.text(
            x,
            y - 0.16,
            subtitle,
            ha="center",
            va="center",
            fontsize=8.6,
            color="#334155",
            zorder=3,
            linespacing=1.35,
        )


def arrow(ax, start, end, label="", color="#334155", style="-|>", dashed=False, rad=0.0, label_offset=(0, 0)):
    patch = FancyArrowPatch(
        start,
        end,
        arrowstyle=style,
        mutation_scale=15,
        linewidth=1.5,
        color=color,
        linestyle="--" if dashed else "-",
        connectionstyle=f"arc3,rad={rad}",
        zorder=1,
        shrinkA=6,
        shrinkB=6,
    )
    ax.add_patch(patch)
    if label:
        mx = (start[0] + end[0]) / 2 + label_offset[0]
        my = (start[1] + end[1]) / 2 + label_offset[1]
        ax.text(
            mx,
            my,
            label,
            ha="center",
            va="center",
            fontsize=8.5,
            color="#1e293b",
            bbox=dict(boxstyle="round,pad=0.22", facecolor="white", edgecolor="none", alpha=0.92),
            zorder=4,
        )


def main() -> None:
    fig, ax = plt.subplots(figsize=(16, 9), dpi=130)
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 9)
    ax.axis("off")
    fig.patch.set_facecolor("#f8fafc")

    ax.text(
        8,
        8.55,
        "PingHub — Gateway-Based Messaging and Notification Platform",
        ha="center",
        va="center",
        fontsize=20,
        fontweight="bold",
        color=COLORS["text"],
    )
    ax.text(
        8,
        8.15,
        "Fast accept path  ·  durable queue  ·  background workers  ·  cache  ·  retries + circuit breaker",
        ha="center",
        va="center",
        fontsize=10.5,
        color="#475569",
    )

    # Nodes
    box(ax, 1.55, 6.55, 2.5, 1.05, "Client / Locust", "POST /messages\nGET /history · GET /messages/:id", COLORS["client"])
    box(ax, 6.15, 6.55, 3.5, 1.25, "API Gateway  :8000", "request_id · routing\nRedis token bucket → 429\n/health aggregation", COLORS["gateway"], 14)
    box(ax, 11.0, 6.55, 2.7, 1.05, "RabbitMQ", "durable queue: messages\nmanagement UI :15672", COLORS["queue"], 13)
    box(ax, 3.2, 3.85, 2.7, 1.1, "Message API  :8001", "history (cache-aside)\ntask status lookup", COLORS["service"])
    box(ax, 7.9, 4.1, 2.9, 1.25, "Worker Pool (2–3)", "consume task · save DB\nnotify · retry 1s/2s/4s\ncircuit breaker", COLORS["worker"], 13)
    box(ax, 12.85, 3.55, 2.4, 1.15, "Notification Mock  :8002", "email/push simulator\nrandom or forced failure", COLORS["service"], 11.5)
    box(ax, 3.2, 1.15, 2.7, 1.05, "Redis", "history cache · task state\nrate buckets · heartbeats", COLORS["data"], 13)
    box(ax, 8.0, 1.15, 3.0, 1.05, "PostgreSQL", "users · messages · task_logs\nmigrations in db/migrations/", COLORS["data"], 13)

    # Arrows
    arrow(ax, (2.82, 6.55), (4.38, 6.55), "requests", label_offset=(0, 0.16))
    arrow(ax, (7.85, 6.45), (9.63, 6.55), "heavy: publish", label_offset=(0, 0.22))
    arrow(ax, (5.55, 6.05), (4.0, 4.42), "light: proxy", rad=-0.12, label_offset=(-0.35, 0.05))
    arrow(ax, (11.0, 6.0), (8.65, 4.72), "deliver task", rad=0.12, label_offset=(0.55, -0.1))
    arrow(ax, (2.85, 3.35), (2.85, 1.7), "cache", label_offset=(0.55, 0))
    arrow(ax, (4.4, 3.35), (6.6, 1.45), "cache miss", rad=-0.1, label_offset=(-0.35, -0.1))
    arrow(ax, (8.55, 3.47), (8.3, 1.7), "read / write", label_offset=(0.75, 0))
    arrow(ax, (12.35, 4.1), (11.7, 3.95), "notify", label_offset=(0, 0.22))
    arrow(ax, (7.0, 4.0), (3.55, 1.75), "invalidate cache", rad=0.1, dashed=True, label_offset=(-0.2, -0.22))

    # Rate limit / health annotation
    arrow(ax, (5.2, 6.2), (3.25, 6.25), "token bucket", rad=0.0, dashed=True, label_offset=(0, 0.2))
    arrow(ax, (5.6, 6.0), (3.2, 1.6), "limits", dashed=True, rad=0.05, label_offset=(0.0, 0.0), color="#64748b")

    # Legend
    ax.text(0.45, 0.42, "Solid = request/data flow      Dashed = cache invalidation / control      Arrow labels describe the action", fontsize=8.5, color="#64748b")

    fig.savefig(OUT, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
