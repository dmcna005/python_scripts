"""Estimate Atlas backup savings from changing snapshot retention, cadence, PITR window and copy regions.

Snapshot storage is incremental: each retained snapshot adds roughly the data that changed since the
previous one, on top of one full baseline copy. Storage is therefore modelled as a retention index

    index = 1.0 (full baseline) + sum(retained copies x delta size as a fraction of a full snapshot)

rather than as a raw copy count, which would wildly overstate the value of deleting hourly snapshots.
Delta assumptions are swept (low/base/high) to produce a range.
"""

from __future__ import annotations

from pathlib import Path

import click
import pandas as pd

# Delta size per retained snapshot, as a fraction of a full snapshot, by snapshot interval.
DELTA_FRACTIONS = {
    "Low": {"hourly": 0.005, "daily": 0.03, "weekly": 0.10, "monthly": 0.18},
    "Base": {"hourly": 0.010, "daily": 0.05, "weekly": 0.15, "monthly": 0.25},
    "High": {"hourly": 0.020, "daily": 0.08, "weekly": 0.22, "monthly": 0.35},
}

# Retained copies per policy item, keyed by delta class. Mirrors the Atlas backup policy UI.
POLICIES: dict[str, dict[str, object]] = {
    "Current (live policy)": {
        "items": {"hourly": 48, "daily": 30, "weekly": 9, "monthly": 12},
        "pitr_days": 7,
        "copy_regions": 2,
    },
    "A. Drop hourly snapshots only": {
        "items": {"hourly": 0, "daily": 30, "weekly": 9, "monthly": 12},
        "pitr_days": 2,
        "copy_regions": 2,
    },
    "B. 24h cadence, daily 7d / weekly 4w / monthly 6m": {
        "items": {"hourly": 0, "daily": 7, "weekly": 4, "monthly": 6},
        "pitr_days": 2,
        "copy_regions": 2,
    },
    "C. Scenario B + one copy region instead of two": {
        "items": {"hourly": 0, "daily": 7, "weekly": 4, "monthly": 6},
        "pitr_days": 2,
        "copy_regions": 1,
    },
    "D. Non-production profile (24h x 3d, weekly 2w, monthly 3m)": {
        "items": {"hourly": 0, "daily": 3, "weekly": 2, "monthly": 3},
        "pitr_days": 2,
        "copy_regions": 0,
    },
}


def retention_index(items: dict[str, int], deltas: dict[str, float]) -> float:
    return 1.0 + sum(copies * deltas[unit] for unit, copies in items.items())


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--snapshot-spend", default=447880.0, show_default=True, help="Annual snapshot storage spend (USD).")
@click.option(
    "--replicated-share",
    default=0.7907,
    show_default=True,
    help="Share of snapshot spend on clusters that copy snapshots to additional regions (CCP-PROD).",
)
@click.option("--pitr-spend", default=426.0, show_default=True, help="Annual continuous backup (PITR) spend (USD).")
@click.option(
    "--support-rate",
    default=0.40,
    show_default=True,
    help="Marginal Atlas support rate applied to the consumption reduction.",
)
@click.option(
    "--outfile",
    default="output_invoices/backup_savings_scenarios.csv",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def main(
    snapshot_spend: float, replicated_share: float, pitr_spend: float, support_rate: float, outfile: Path
) -> None:
    baseline = POLICIES["Current (live policy)"]
    baseline_items: dict[str, int] = baseline["items"]  # type: ignore[assignment]
    baseline_pitr = float(baseline["pitr_days"])  # type: ignore[arg-type]
    baseline_regions = int(baseline["copy_regions"])  # type: ignore[arg-type]

    replicated_spend = snapshot_spend * replicated_share
    single_region_spend = snapshot_spend - replicated_spend

    rows: list[dict[str, object]] = []
    for name, policy in POLICIES.items():
        items: dict[str, int] = policy["items"]  # type: ignore[assignment]
        pitr_days = float(policy["pitr_days"])  # type: ignore[arg-type]
        regions = int(policy["copy_regions"])  # type: ignore[arg-type]

        for band, deltas in DELTA_FRACTIONS.items():
            retention_factor = retention_index(items, deltas) / retention_index(baseline_items, deltas)
            region_factor = (1 + regions) / (1 + baseline_regions)

            new_spend = replicated_spend * retention_factor * region_factor + single_region_spend * retention_factor
            storage_saving = snapshot_spend - new_spend
            pitr_saving = pitr_spend * (1 - pitr_days / baseline_pitr)
            backup_saving = storage_saving + pitr_saving

            rows.append(
                {
                    "Scenario": name,
                    "Band": band,
                    "Retention Index": round(retention_index(items, deltas), 2),
                    "Storage Reduction %": round((1 - new_spend / snapshot_spend) * 100, 1),
                    "Copy Regions": regions,
                    "PITR Window (days)": pitr_days,
                    "Snapshot Storage Saving": round(storage_saving, 2),
                    "PITR Saving": round(pitr_saving, 2),
                    "Backup Saving": round(backup_saving, 2),
                    "Support Saving": round(backup_saving * support_rate, 2),
                    "Total Annual Saving": round(backup_saving * (1 + support_rate), 2),
                }
            )

    results = pd.DataFrame(rows)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(outfile, index=False)

    click.echo(results.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    click.echo(f"\nSaved: {outfile.resolve()}")


if __name__ == "__main__":
    main()
