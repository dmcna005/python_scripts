"""Multi-invoice MongoDB Atlas cost analysis, EDA and 12-month ML forecasting."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import click
import httpx
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

CANONICAL_COLUMNS = [
    "Billed Date",
    "Organization ID",
    "Project Name",
    "Cluster Name",
    "Service",
    "Sub Service",
    "Region",
    "Cost",
]

# FOCUS-style Atlas invoice export -> canonical names. First match wins.
FOCUS_COLUMN_MAP: dict[str, list[str]] = {
    "Billed Date": ["x_BilledDateStart", "ChargePeriodStart", "BillingPeriodStart"],
    "Organization ID": ["SubAccountId"],
    "Project Name": ["SubAccountName", "ResourceName", "BillingAccountName"],
    "C≤≥luster Name": ["ResourceName", "SubAccountName"],
    "Service": ["x_SkuCategory", "SkuMeter", "ServiceCategory"],
    "Sub Service": ["SkuMeter", "ChargeDescription"],
    "Region": ["RegionName", "RegionId"],
    "Cost": ["BilledCost", "EffectiveCost", "ContractedCost", "ListCost"],
}

LEGACY_COLUMN_MAP: dict[str, list[str]] = {
    "Billed Date": ["Billed Date"],
    "Organization ID": ["Organization ID", "OrganizationId", "SubAccountId"],
    "Project Name": ["Project Name"],
    "Cluster Name": ["Cluster Name"],
    "Service": ["Service"],
    "Sub Service": ["Sku", "SKU", "Service"],
    "Region": ["Region", "Region Name"],
    "Cost": ["Usage Amount", "Cost", "Total Cost"],
}

FORECAST_HORIZON_MONTHS = 12
EXCEL_SUFFIXES = {".xls", ".xlsx", ".xlsm", ".xlsb"}


def read_billing_file(file: Path) -> pd.DataFrame:
    if file.suffix.lower() in EXCEL_SUFFIXES:
        return pd.read_excel(file)
    return pd.read_csv(file, low_memory=False)


async def download_invoice_files(urls: tuple[str, ...], directory: Path) -> list[Path]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        async def download(index: int, url: str) -> Path:
            parsed = httpx.URL(url)
            if parsed.scheme not in {"http", "https"} or not parsed.host:
                raise click.BadParameter("Invoice URLs must use HTTP or HTTPS.", param_hint="--url")
            response = await client.get(url)
            response.raise_for_status()
            filename = Path(parsed.path).name or "invoice.csv"
            file = directory / f"{index}_{filename}"
            file.write_bytes(response.content)
            return file

        return list(await asyncio.gather(*(download(index, url) for index, url in enumerate(urls))))


def normalize_projects(project_values: tuple[str, ...]) -> list[str]:
    projects: list[str] = []
    for raw in project_values:
        projects.extend(part.strip() for part in raw.split(",") if part.strip())

    seen: set[str] = set()
    deduped: list[str] = []
    for project in projects:
        if project not in seen:
            seen.add(project)
            deduped.append(project)
    return deduped


def discover_input_files(paths: tuple[Path, ...]) -> list[Path]:
    """Expand files/directories into a sorted list of invoice files."""
    candidates: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved.is_dir():
            for child in sorted(resolved.iterdir()):
                if child.is_file() and not child.name.startswith("."):
                    candidates.append(child)
        else:
            candidates.append(resolved)

    # Drop .gz duplicates when the uncompressed twin is present.
    files = [c for c in candidates if not (c.suffix == ".gz" and c.with_suffix("") in candidates)]

    unique: list[Path] = []
    seen: set[Path] = set()
    for file in files:
        if file not in seen:
            seen.add(file)
            unique.append(file)
    return unique


def _resolve_mapping(columns: list[str]) -> dict[str, str]:
    """Pick the column mapping that matches the file layout."""
    column_set = set(columns)
    for mapping in (FOCUS_COLUMN_MAP, LEGACY_COLUMN_MAP):
        resolved: dict[str, str] = {}
        for canonical, candidates in mapping.items():
            for candidate in candidates:
                if candidate in column_set:
                    resolved[canonical] = candidate
                    break
        if {"Billed Date", "Project Name", "Service", "Cost"} <= resolved.keys():
            return resolved
    raise ValueError("Unrecognised invoice layout: no known cost/date/project columns found.")


def load_invoice_file(file: Path) -> pd.DataFrame:
    raw = read_billing_file(file)
    raw.columns = [str(col).strip() for col in raw.columns]
    mapping = _resolve_mapping(list(raw.columns))

    df = pd.DataFrame(index=raw.index)
    for canonical in CANONICAL_COLUMNS:
        source = mapping.get(canonical)
        df[canonical] = raw[source] if source else pd.NA

    df["Source File"] = file.name
    return df


def load_cost_data(files: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load, concatenate and clean every invoice file. Returns (clean df, cleaning log)."""
    frames: list[pd.DataFrame] = []
    log_rows: list[dict[str, object]] = []

    for file in files:
        try:
            frame = load_invoice_file(file)
        except (ValueError, OSError, ImportError) as exc:
            log_rows.append({"Source File": file.name, "Stage": "skipped", "Rows": 0, "Detail": str(exc)})
            continue
        log_rows.append({"Source File": file.name, "Stage": "loaded", "Rows": len(frame), "Detail": ""})
        frames.append(frame)

    if not frames:
        raise click.ClickException("No readable invoice files were found.")

    df = pd.concat(frames, ignore_index=True)
    rows_in = len(df)

    for col in ["Organization ID", "Project Name", "Cluster Name", "Service", "Sub Service", "Region"]:
        df[col] = df[col].astype("string").str.strip()
        df[col] = df[col].replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})

    df["Billed Date"] = pd.to_datetime(df["Billed Date"], errors="coerce", utc=True).dt.tz_localize(None)
    df["Cost"] = pd.to_numeric(df["Cost"], errors="coerce")

    missing_dates = int(df["Billed Date"].isna().sum())
    missing_costs = int(df["Cost"].isna().sum())
    df = df.dropna(subset=["Billed Date", "Cost"])
    df = df[np.isfinite(df["Cost"])]

    df["Organization ID"] = df["Organization ID"].fillna("Unknown Organization")
    df["Project Name"] = df["Project Name"].fillna("Unknown Project")
    df["Cluster Name"] = df["Cluster Name"].fillna(df["Project Name"])
    df["Service"] = df["Service"].fillna("Unknown Service")
    df["Sub Service"] = df["Sub Service"].fillna(df["Service"])
    df["Region"] = df["Region"].fillna("Unknown Region")

    before_dupes = len(df)
    df = df.drop_duplicates()
    duplicates_removed = before_dupes - len(df)

    negative_rows = int((df["Cost"] < 0).sum())

    df["Month Start"] = df["Billed Date"].dt.to_period("M").dt.to_timestamp()
    df["Month"] = df["Month Start"].dt.strftime("%Y-%m")

    log_rows.extend(
        [
            {"Source File": "<all>", "Stage": "rows in", "Rows": rows_in, "Detail": ""},
            {"Source File": "<all>", "Stage": "invalid dates dropped", "Rows": missing_dates, "Detail": ""},
            {"Source File": "<all>", "Stage": "invalid costs dropped", "Rows": missing_costs, "Detail": ""},
            {"Source File": "<all>", "Stage": "duplicate rows dropped", "Rows": duplicates_removed, "Detail": ""},
            {
                "Source File": "<all>",
                "Stage": "negative cost rows",
                "Rows": negative_rows,
                "Detail": "credits/refunds retained",
            },
            {"Source File": "<all>", "Stage": "rows out", "Rows": len(df), "Detail": ""},
        ]
    )

    if df.empty:
        raise click.ClickException("All invoice rows were removed during cleaning.")

    return df.reset_index(drop=True), pd.DataFrame(log_rows)


def build_summary_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Spend rollups: per project, per project+service, per service and overall."""
    total = float(df["Cost"].sum())

    by_organization = (
        df.groupby(["Organization ID", "Project Name"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Total Spend"})
        .sort_values("Total Spend", ascending=False)
    )
    by_organization["% of Total"] = by_organization["Total Spend"] / total * 100 if total else 0.0

    by_project = (
        df.groupby("Project Name", as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Total Spend"})
        .sort_values("Total Spend", ascending=False)
    )
    by_project["% of Total"] = by_project["Total Spend"] / total * 100 if total else 0.0

    by_project_service = (
        df.groupby(["Project Name", "Service"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Total Spend"})
        .sort_values(["Project Name", "Total Spend"], ascending=[True, False])
    )
    project_totals = by_project_service.groupby("Project Name")["Total Spend"].transform("sum")
    by_project_service["% of Project"] = np.where(
        project_totals != 0, by_project_service["Total Spend"] / project_totals * 100, 0.0
    )
    by_project_service["% of Total"] = by_project_service["Total Spend"] / total * 100 if total else 0.0

    by_service = (
        df.groupby("Service", as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Total Spend"})
        .sort_values("Total Spend", ascending=False)
    )
    by_service["% of Total"] = by_service["Total Spend"] / total * 100 if total else 0.0

    by_sub_service = (
        df.groupby(["Service", "Sub Service"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Total Spend"})
        .sort_values("Total Spend", ascending=False)
    )

    by_cluster = (
        df.groupby(["Project Name", "Cluster Name", "Service"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Total Spend"})
        .sort_values(["Project Name", "Cluster Name", "Total Spend"], ascending=[True, True, False])
    )

    monthly_total = (
        df.groupby("Month", as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Monthly Spend"})
        .sort_values("Month")
    )
    monthly_project = (
        df.groupby(["Month", "Project Name"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Monthly Spend"})
        .sort_values(["Month", "Project Name"])
    )
    monthly_service = (
        df.groupby(["Month", "Service"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Monthly Spend"})
        .sort_values(["Month", "Service"])
    )

    months = df["Month"].nunique()
    overall = pd.DataFrame(
        [
            {"Metric": "Total spend (all projects and services)", "Value": f"{total:,.2f}"},
            {"Metric": "Months covered", "Value": str(months)},
            {"Metric": "Average monthly spend", "Value": f"{total / months:,.2f}" if months else "0.00"},
            {"Metric": "Median monthly spend", "Value": f"{monthly_total['Monthly Spend'].median():,.2f}"},
            {"Metric": "Highest monthly spend", "Value": f"{monthly_total['Monthly Spend'].max():,.2f}"},
            {"Metric": "Lowest monthly spend", "Value": f"{monthly_total['Monthly Spend'].min():,.2f}"},
            {"Metric": "Projects", "Value": str(df["Project Name"].nunique())},
            {"Metric": "Services", "Value": str(df["Service"].nunique())},
            {"Metric": "Line items", "Value": f"{len(df):,}"},
        ]
    )

    return {
        "total_spend_by_organization": by_organization,
        "total_spend_by_project": by_project,
        "spend_by_project_and_service": by_project_service,
        "spend_by_service": by_service,
        "spend_by_sub_service": by_sub_service,
        "spend_by_project_cluster_service": by_cluster,
        "monthly_spend_total": monthly_total,
        "monthly_spend_by_project": monthly_project,
        "monthly_spend_by_service": monthly_service,
        "overall_summary": overall,
    }


def run_eda(df: pd.DataFrame, cleaning_log: pd.DataFrame, outdir: Path) -> tuple[Path, list[tuple[str, plt.Figure]]]:
    """Write an EDA text report and build exploratory figures."""
    monthly = df.groupby("Month")["Cost"].sum()

    lines = [
        "=== DATA OVERVIEW ===",
        f"Line items: {len(df):,}",
        f"Date range: {df['Billed Date'].min():%Y-%m-%d} to {df['Billed Date'].max():%Y-%m-%d}",
        f"Invoice files: {df['Source File'].nunique()} ({', '.join(sorted(df['Source File'].unique()))})",
        f"Months covered: {df['Month'].nunique()}",
        f"Projects: {', '.join(sorted(df['Project Name'].unique()))}",
        f"Services: {', '.join(sorted(df['Service'].unique()))}",
        "",
        "=== CLEANING LOG ===",
        cleaning_log.to_string(index=False),
        "",
        "=== COST LINE-ITEM DISTRIBUTION ===",
        df["Cost"].describe().to_string(),
        f"Skewness: {df['Cost'].skew():.3f}",
        f"Rows with zero cost: {int((df['Cost'] == 0).sum()):,}",
        f"Rows with negative cost: {int((df['Cost'] < 0).sum()):,}",
        "",
        "=== MONTHLY TOTALS ===",
        monthly.round(2).to_string(),
        f"Month-over-month average change: {monthly.pct_change().mean() * 100:.2f}%",
        "",
        "=== MISSING VALUES (post-clean) ===",
        df[CANONICAL_COLUMNS].isna().sum().to_string(),
    ]

    eda_path = outdir / "eda_report.txt"
    eda_path.write_text("\n".join(lines), encoding="utf-8")

    sns.set_theme(style="whitegrid")
    figures: list[tuple[str, plt.Figure]] = []

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    positive = df.loc[df["Cost"] > 0, "Cost"]
    sns.histplot(positive, bins=50, log_scale=True, ax=axes[0], color="steelblue")
    axes[0].set_title("Line-item cost distribution (log scale)")
    axes[0].set_xlabel("Cost (USD)")
    sns.boxplot(data=df, x="Service", y="Cost", ax=axes[1])
    axes[1].set_yscale("symlog")
    axes[1].set_title("Cost spread by service")
    axes[1].tick_params(axis="x", rotation=30)
    fig.tight_layout()
    figures.append(("eda_cost_distribution", fig))

    pivot = df.pivot_table(index="Project Name", columns="Service", values="Cost", aggfunc="sum", fill_value=0.0)
    fig, ax = plt.subplots(figsize=(12, max(4.0, 0.7 * len(pivot) + 2)))
    sns.heatmap(pivot, annot=True, fmt=",.0f", cmap="YlOrRd", ax=ax, cbar_kws={"label": "Spend (USD)"})
    ax.set_title("Spend heatmap: project x service")
    fig.tight_layout()
    figures.append(("eda_project_service_heatmap", fig))

    return eda_path, figures


def add_time_features(df: pd.DataFrame, origin_month: pd.Timestamp, month_col: str = "Month Start") -> pd.DataFrame:
    result = df.copy()
    result[month_col] = pd.to_datetime(result[month_col])
    result["Month Number"] = result[month_col].dt.month
    result["Month Index"] = (result[month_col].dt.year - origin_month.year) * 12 + (
        result[month_col].dt.month - origin_month.month
    )
    result["Month Sin"] = np.sin(2 * np.pi * result["Month Number"] / 12)
    result["Month Cos"] = np.cos(2 * np.pi * result["Month Number"] / 12)
    return result


def split_by_time(data: pd.DataFrame, time_col: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    unique_times = sorted(data[time_col].dropna().unique())
    n_times = len(unique_times)
    if n_times < 3:
        raise click.ClickException("Need at least 3 distinct billing months to train a forecast model.")

    if n_times == 3:
        train_end, val_end = 1, 2
    elif n_times == 4:
        train_end, val_end = 2, 3
    else:
        train_end = max(1, int(n_times * 0.6))
        val_end = min(max(train_end + 1, int(n_times * 0.8)), n_times - 1)

    train_df = data[data[time_col].isin(unique_times[:train_end])].copy()
    val_df = data[data[time_col].isin(unique_times[train_end:val_end])].copy()
    test_df = data[data[time_col].isin(unique_times[val_end:])].copy()
    return train_df, val_df, test_df


def evaluate_models(
    data: pd.DataFrame,
    target_col: str,
    cat_cols: list[str],
    num_cols: list[str],
    time_col: str,
) -> tuple[Pipeline, pd.DataFrame, dict[str, object]]:
    train_df, val_df, test_df = split_by_time(data, time_col=time_col)

    feature_cols = cat_cols + num_cols
    x_train, y_train = train_df[feature_cols], train_df[target_col]
    x_val, y_val = val_df[feature_cols], val_df[target_col]
    x_test, y_test = test_df[feature_cols], test_df[target_col]

    preprocessor = ColumnTransformer(
        [("cat", OneHotEncoder(handle_unknown="ignore"), cat_cols), ("num", "passthrough", num_cols)]
    )

    models = {
        "Ridge": Ridge(alpha=1.0),
        "RandomForest": RandomForestRegressor(n_estimators=400, random_state=42, n_jobs=-1),
        "ExtraTrees": ExtraTreesRegressor(n_estimators=400, random_state=42, n_jobs=-1),
        "GradientBoosting": GradientBoostingRegressor(random_state=42),
    }

    model_rows: list[dict[str, object]] = []
    best_model_name = ""
    best_pipeline: Pipeline | None = None
    best_val_rmse = float("inf")

    for model_name, model in models.items():
        pipeline = Pipeline(steps=[("preprocessor", preprocessor), ("model", model)])
        pipeline.fit(x_train, y_train)

        val_pred = np.clip(pipeline.predict(x_val), 0, None)
        test_pred = np.clip(pipeline.predict(x_test), 0, None)
        val_rmse = float(np.sqrt(mean_squared_error(y_val, val_pred)))

        model_rows.append(
            {
                "Model": model_name,
                "Validation MAE": mean_absolute_error(y_val, val_pred),
                "Validation RMSE": val_rmse,
                "Validation R2": r2_score(y_val, val_pred),
                "Test MAE": mean_absolute_error(y_test, test_pred),
                "Test RMSE": float(np.sqrt(mean_squared_error(y_test, test_pred))),
                "Test R2": r2_score(y_test, test_pred),
            }
        )

        if val_rmse < best_val_rmse:
            best_val_rmse, best_model_name, best_pipeline = val_rmse, model_name, pipeline

    assert best_pipeline is not None

    combined = pd.concat([train_df, val_df], ignore_index=True)
    best_pipeline.fit(combined[feature_cols], combined[target_col])
    final_test_pred = np.clip(best_pipeline.predict(x_test), 0, None)
    final_metrics: dict[str, object] = {
        "Best Model": best_model_name,
        "Final Test MAE": float(mean_absolute_error(y_test, final_test_pred)),
        "Final Test RMSE": float(np.sqrt(mean_squared_error(y_test, final_test_pred))),
        "Final Test R2": float(r2_score(y_test, final_test_pred)),
        "Train Months": int(train_df[time_col].nunique()),
        "Validation Months": int(val_df[time_col].nunique()),
        "Test Months": int(test_df[time_col].nunique()),
    }

    metrics_df = pd.DataFrame(model_rows).sort_values("Validation RMSE").reset_index(drop=True)
    metrics_df["Selected"] = metrics_df["Model"] == best_model_name

    # Refit the winning model on every month so the forecast uses all available history.
    best_pipeline.fit(data[feature_cols], data[target_col])
    return best_pipeline, metrics_df, final_metrics


def build_organization_scenarios(organization_service: pd.DataFrame, buffer: float) -> pd.DataFrame:
    """Build current, buffered, and cost-optimized 12-month budgets by organization."""
    backup_reduction = 0.653
    negotiated_support_rate = 0.40
    rows: list[dict[str, object]] = []

    for (organization_id, project_name), group in organization_service.groupby(["Organization ID", "Project Name"]):
        current = float(group["Predicted Spend"].sum())
        backup = float(group.loc[group["Service"] == "Backup", "Predicted Spend"].sum())
        support = float(group.loc[group["Service"] == "Support", "Predicted Spend"].sum())
        optimized_backup = backup * (1 - backup_reduction)
        optimized_consumption = current - support - backup + optimized_backup
        optimized_support = optimized_consumption * negotiated_support_rate
        optimized = optimized_consumption + optimized_support

        rows.append(
            {
                "Organization ID": organization_id,
                "Project Name": project_name,
                "Current 12M Forecast": current,
                f"Current Forecast + {buffer:.0%} Buffer": current * (1 + buffer),
                f"Optimized Forecast + {buffer:.0%} Buffer": optimized * (1 + buffer),
                "Estimated Annual Savings vs Buffered Current": current * (1 + buffer) - optimized * (1 + buffer),
                "Backup Reduction Assumption": backup_reduction,
                "Negotiated Support Rate": negotiated_support_rate,
            }
        )

    return pd.DataFrame(rows).sort_values("Current 12M Forecast", ascending=False)


def forecast_next_12_months(df: pd.DataFrame, buffer: float) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    """Train on the monthly project x service panel and project the next 12 months."""
    panel = (
        df.groupby(["Month Start", "Organization ID", "Project Name", "Service"], as_index=False)["Cost"]
        .sum()
        .rename(columns={"Cost": "Spend"})
    )

    # Balance the panel so every project/service has a value in every month.
    months = pd.DataFrame({"Month Start": sorted(panel["Month Start"].unique())})
    combos = panel[["Organization ID", "Project Name", "Service"]].drop_duplicates()
    panel = (
        combos.merge(months, how="cross")
        .merge(panel, on=["Month Start", "Organization ID", "Project Name", "Service"], how="left")
        .fillna({"Spend": 0.0})
    )

    origin_month = pd.Timestamp(panel["Month Start"].min()).replace(day=1)
    panel = add_time_features(panel, origin_month)

    cat_cols = ["Organization ID", "Project Name", "Service"]
    num_cols = ["Month Index", "Month Number", "Month Sin", "Month Cos"]
    pipeline, metrics_df, final_metrics = evaluate_models(
        data=panel, target_col="Spend", cat_cols=cat_cols, num_cols=num_cols, time_col="Month Start"
    )

    last_month = pd.Timestamp(panel["Month Start"].max())
    future_months = pd.date_range(
        start=last_month + pd.offsets.MonthBegin(1), periods=FORECAST_HORIZON_MONTHS, freq="MS"
    )
    future = combos.merge(pd.DataFrame({"Month Start": future_months}), how="cross")
    future = add_time_features(future, origin_month)
    future["Predicted Spend"] = np.clip(pipeline.predict(future[cat_cols + num_cols]), 0, None)
    future["Month"] = future["Month Start"].dt.strftime("%Y-%m")

    organization_service = future.groupby(
        ["Organization ID", "Project Name", "Service"], as_index=False
    )["Predicted Spend"].sum()
    organization_scenarios = build_organization_scenarios(organization_service, buffer)

    by_project_service = (
        future.groupby(["Project Name", "Service"], as_index=False)["Predicted Spend"]
        .sum()
        .rename(columns={"Predicted Spend": "Predicted 12M Spend"})
        .sort_values(["Project Name", "Predicted 12M Spend"], ascending=[True, False])
    )

    monthly_by_project = (
        future.groupby(["Month", "Project Name"], as_index=False)["Predicted Spend"]
        .sum()
        .sort_values(["Month", "Project Name"])
    )

    history_by_project = (
        df.groupby("Project Name", as_index=False)["Cost"].sum().rename(columns={"Cost": "Historical Spend"})
    )
    buffer_col = f"Budget With {buffer:.0%} Buffer"
    by_project = (
        future.groupby("Project Name", as_index=False)["Predicted Spend"]
        .sum()
        .rename(columns={"Predicted Spend": "Predicted 12M Spend"})
        .merge(history_by_project, on="Project Name", how="outer")
        .fillna({"Predicted 12M Spend": 0.0, "Historical Spend": 0.0})
    )
    by_project["Predicted Avg Monthly Spend"] = by_project["Predicted 12M Spend"] / FORECAST_HORIZON_MONTHS
    by_project[buffer_col] = by_project["Predicted 12M Spend"] * (1 + buffer)
    by_project = by_project.sort_values("Predicted 12M Spend", ascending=False)

    overall_monthly = future.groupby("Month", as_index=False)["Predicted Spend"].sum().sort_values("Month")
    overall_monthly["Cumulative Predicted Spend"] = overall_monthly["Predicted Spend"].cumsum()

    overall_total = float(future["Predicted Spend"].sum())
    historical_total = float(df["Cost"].sum())
    months_observed = df["Month"].nunique()
    overall_projection = pd.DataFrame(
        [
            {"Metric": "Historical total spend", "Value": f"{historical_total:,.2f}"},
            {"Metric": "Historical average monthly spend", "Value": f"{historical_total / months_observed:,.2f}"},
            {"Metric": "Forecast window", "Value": f"{future['Month'].min()} to {future['Month'].max()}"},
            {"Metric": "Predicted next-12-month spend (overall)", "Value": f"{overall_total:,.2f}"},
            {"Metric": "Predicted average monthly spend", "Value": f"{overall_total / FORECAST_HORIZON_MONTHS:,.2f}"},
            {"Metric": f"Recommended budget with {buffer:.0%} buffer", "Value": f"{overall_total * (1 + buffer):,.2f}"},
            {"Metric": "Model used", "Value": str(final_metrics["Best Model"])},
            {"Metric": "Final test RMSE", "Value": f"{float(final_metrics['Final Test RMSE']):,.2f}"},
            {"Metric": "Final test R2", "Value": f"{float(final_metrics['Final Test R2']):.4f}"},
        ]
    )

    tables = {
        "forecast_12m_by_organization": organization_scenarios,
        "forecast_12m_by_project": by_project,
        "forecast_12m_by_project_and_service": by_project_service,
        "forecast_monthly_by_project": monthly_by_project,
        "forecast_monthly_overall": overall_monthly,
        "forecast_overall_projection": overall_projection,
        "ml_model_comparison": metrics_df,
    }
    return tables, final_metrics


def build_final_report(
    summaries: dict[str, pd.DataFrame], forecast_by_project: pd.DataFrame, buffer: float
) -> pd.DataFrame:
    """One consolidated CSV: historical spend and 12-month forecast per project/service plus totals."""
    buffer_col = f"Budget With {buffer:.0%} Buffer"

    detail = summaries["spend_by_project_and_service"][["Project Name", "Service", "Total Spend"]].rename(
        columns={"Total Spend": "Historical Spend"}
    )
    detail["Scope"] = "Project x Service"

    project_rows = summaries["total_spend_by_project"][["Project Name", "Total Spend"]].rename(
        columns={"Total Spend": "Historical Spend"}
    )
    if not forecast_by_project.empty:
        project_rows = project_rows.merge(
            forecast_by_project[["Project Name", "Predicted 12M Spend", buffer_col]], on="Project Name", how="left"
        )
    project_rows["Service"] = "ALL SERVICES"
    project_rows["Scope"] = "Project total"

    service_rows = summaries["spend_by_service"][["Service", "Total Spend"]].rename(
        columns={"Total Spend": "Historical Spend"}
    )
    service_rows["Project Name"] = "ALL PROJECTS"
    service_rows["Scope"] = "Service total"

    grand = pd.DataFrame(
        [
            {
                "Scope": "Grand total",
                "Project Name": "ALL PROJECTS",
                "Service": "ALL SERVICES",
                "Historical Spend": float(summaries["total_spend_by_project"]["Total Spend"].sum()),
                "Predicted 12M Spend": float(forecast_by_project["Predicted 12M Spend"].sum())
                if not forecast_by_project.empty
                else np.nan,
                buffer_col: float(forecast_by_project[buffer_col].sum()) if not forecast_by_project.empty else np.nan,
            }
        ]
    )

    final = pd.concat([detail, project_rows, service_rows, grand], ignore_index=True)
    cols = ["Scope", "Project Name", "Service", "Historical Spend", "Predicted 12M Spend", buffer_col]
    for col in cols:
        if col not in final.columns:
            final[col] = np.nan
    return final[cols]


def build_charts(
    summaries: dict[str, pd.DataFrame], forecast_tables: dict[str, pd.DataFrame] | None
) -> list[tuple[str, plt.Figure]]:
    sns.set_theme(style="whitegrid")
    figures: list[tuple[str, plt.Figure]] = []

    fig, ax = plt.subplots(figsize=(12, 6))
    organization_spend = summaries["total_spend_by_organization"].copy()
    organization_spend["Organization"] = (
        organization_spend["Project Name"] + " (" + organization_spend["Organization ID"] + ")"
    )
    sns.barplot(
        data=organization_spend,
        x="Total Spend",
        y="Organization",
        hue="Organization",
        legend=False,
        ax=ax,
    )
    ax.set_title("Total historical spend by organization ID")
    ax.set_xlabel("Spend (USD)")
    for container in ax.containers:
        ax.bar_label(container, labels=[f"${value / 1_000_000:.2f}M" for value in container.datavalues], padding=4)
    ax.set_xlim(0, summaries["total_spend_by_organization"]["Total Spend"].max() * 1.15)
    fig.tight_layout()
    figures.append(("total_spend_by_organization", fig))

    fig, ax = plt.subplots(figsize=(13, 6))
    pivot = (
        summaries["monthly_spend_by_service"]
        .pivot(index="Month", columns="Service", values="Monthly Spend")
        .fillna(0.0)
    )
    pivot.plot(kind="bar", stacked=True, ax=ax, colormap="tab20")
    ax.set_title("Monthly spend by service")
    ax.set_ylabel("Spend (USD)")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    figures.append(("monthly_spend_by_service", fig))

    fig, ax = plt.subplots(figsize=(13, 6))
    pivot = (
        summaries["monthly_spend_by_project"]
        .pivot(index="Month", columns="Project Name", values="Monthly Spend")
        .fillna(0.0)
    )
    pivot.plot(kind="bar", stacked=True, ax=ax, colormap="tab10")
    ax.set_title("Monthly spend by project")
    ax.set_ylabel("Spend (USD)")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    figures.append(("monthly_spend_by_project", fig))

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    sns.barplot(
        data=summaries["total_spend_by_project"],
        x="Total Spend",
        y="Project Name",
        hue="Project Name",
        legend=False,
        ax=axes[0],
    )
    axes[0].set_title("Total spend by project")
    sns.barplot(
        data=summaries["spend_by_service"], x="Total Spend", y="Service", hue="Service", legend=False, ax=axes[1]
    )
    axes[1].set_title("Total spend by service")
    fig.tight_layout()
    figures.append(("total_spend_by_project_and_service", fig))

    fig, ax = plt.subplots(figsize=(12, 7))
    sns.barplot(
        data=summaries["spend_by_sub_service"].head(15),
        x="Total Spend",
        y="Sub Service",
        hue="Service",
        dodge=False,
        ax=ax,
    )
    ax.set_title("Top 15 cost drivers (SKU level)")
    fig.tight_layout()
    figures.append(("top_cost_drivers", fig))

    if forecast_tables is None:
        return figures

    organization_scenarios = forecast_tables["forecast_12m_by_organization"]
    scenario_columns = [
        column
        for column in organization_scenarios.columns
        if column.startswith("Current 12M")
        or column.startswith("Current Forecast +")
        or column.startswith("Optimized Forecast +")
    ]
    organization_scenarios["Organization"] = (
        organization_scenarios["Project Name"] + " (" + organization_scenarios["Organization ID"] + ")"
    )
    scenario_chart = organization_scenarios.melt(
        id_vars="Organization",
        value_vars=scenario_columns,
        var_name="Scenario",
        value_name="Annual Spend",
    )
    fig, ax = plt.subplots(figsize=(13, 6))
    sns.barplot(data=scenario_chart, x="Annual Spend", y="Organization", hue="Scenario", ax=ax)
    ax.set_title("Organization ID 12-month forecast scenarios")
    ax.set_xlabel("Annual spend (USD)")
    for container in ax.containers:
        ax.bar_label(container, labels=[f"${value / 1_000_000:.2f}M" for value in container.datavalues], padding=4)
    ax.set_xlim(0, scenario_chart["Annual Spend"].max() * 1.15)
    fig.tight_layout()
    figures.append(("forecast_scenarios_by_organization", fig))

    history = summaries["monthly_spend_total"]
    future = forecast_tables["forecast_monthly_overall"]
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(history["Month"], history["Monthly Spend"], marker="o", label="Actual")
    ax.plot(
        future["Month"],
        future["Predicted Spend"],
        marker="o",
        linestyle="--",
        color="firebrick",
        label="Forecast (next 12 months)",
    )
    ax.set_title("Overall monthly spend: history and 12-month forecast")
    ax.set_ylabel("Spend (USD)")
    ax.tick_params(axis="x", rotation=45)
    ax.legend()
    fig.tight_layout()
    figures.append(("forecast_overall_trend", fig))

    fig, ax = plt.subplots(figsize=(14, 6))
    pivot = (
        forecast_tables["forecast_monthly_by_project"]
        .pivot(index="Month", columns="Project Name", values="Predicted Spend")
        .fillna(0.0)
    )
    pivot.plot(kind="bar", stacked=True, ax=ax, colormap="tab10")
    ax.set_title("Forecast for the next 12 months by project")
    ax.set_ylabel("Predicted spend (USD)")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    figures.append(("forecast_monthly_by_project", fig))

    fig, ax = plt.subplots(figsize=(12, 6))
    melted = forecast_tables["forecast_12m_by_project"].melt(
        id_vars="Project Name",
        value_vars=["Historical Spend", "Predicted 12M Spend"],
        var_name="Period",
        value_name="Spend",
    )
    sns.barplot(data=melted, x="Spend", y="Project Name", hue="Period", ax=ax)
    ax.set_title("Historical spend vs predicted 12-month spend by project")
    fig.tight_layout()
    figures.append(("forecast_vs_history_by_project", fig))

    fig, ax = plt.subplots(figsize=(11, 6))
    sns.barplot(
        data=forecast_tables["ml_model_comparison"],
        x="Model",
        y="Validation RMSE",
        hue="Model",
        legend=False,
        ax=ax,
    )
    ax.set_title("ML model comparison (lower validation RMSE is better)")
    fig.tight_layout()
    figures.append(("ml_model_comparison", fig))

    return figures


def _format_table_for_pdf(df: pd.DataFrame) -> pd.DataFrame:
    formatted = df.copy()
    for col in formatted.columns:
        if pd.api.types.is_numeric_dtype(formatted[col]):
            formatted[col] = formatted[col].map(lambda x: "" if pd.isna(x) else f"{x:,.2f}")
        else:
            formatted[col] = formatted[col].astype(str)
    return formatted


def _add_table_pages(pdf: PdfPages, df: pd.DataFrame, title: str, rows_per_page: int = 28) -> None:
    formatted = _format_table_for_pdf(df)
    total_rows = len(formatted)
    if total_rows == 0:
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        ax.axis("off")
        ax.set_title(f"{title} (No Data)", fontsize=14, pad=20)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)
        return

    for start in range(0, total_rows, rows_per_page):
        end = min(start + rows_per_page, total_rows)
        page_df = formatted.iloc[start:end]

        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        ax.axis("off")
        ax.set_title(f"{title} (rows {start + 1}-{end} of {total_rows})", fontsize=14, pad=20)
        table = ax.table(cellText=page_df.values, colLabels=page_df.columns, loc="center", cellLoc="left")
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 1.2)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)


def save_figures(figures: list[tuple[str, plt.Figure]], outdir: Path) -> list[Path]:
    charts_dir = outdir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, fig in figures:
        path = charts_dir / f"{name}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        paths.append(path)
    return paths


def create_pdf_report(
    outdir: Path,
    figures: list[tuple[str, plt.Figure]],
    summaries: dict[str, pd.DataFrame],
    final_report: pd.DataFrame,
    forecast_tables: dict[str, pd.DataFrame] | None,
) -> Path:
    pdf_path = outdir / "cost_report.pdf"
    with PdfPages(pdf_path) as pdf:
        for _, fig in figures:
            pdf.savefig(fig, bbox_inches="tight")
        _add_table_pages(pdf, summaries["overall_summary"], "Overall Spend Summary")
        _add_table_pages(pdf, summaries["total_spend_by_organization"], "Total Spend by Organization")
        _add_table_pages(pdf, summaries["total_spend_by_project"], "Total Spend by Project")
        _add_table_pages(pdf, summaries["spend_by_service"], "Total Spend by Service")
        _add_table_pages(pdf, summaries["spend_by_project_and_service"], "Spend by Project and Service")
        if forecast_tables is not None:
            _add_table_pages(pdf, forecast_tables["forecast_overall_projection"], "Overall 12-Month Projection")
            _add_table_pages(
                pdf, forecast_tables["forecast_12m_by_organization"], "12-Month Scenarios by Organization"
            )
            _add_table_pages(pdf, forecast_tables["forecast_12m_by_project"], "12-Month Forecast by Project")
            _add_table_pages(
                pdf, forecast_tables["forecast_12m_by_project_and_service"], "12-Month Forecast by Project and Service"
            )
            _add_table_pages(pdf, forecast_tables["ml_model_comparison"], "ML Model Comparison")
        _add_table_pages(pdf, final_report, "Final Consolidated Report")
    return pdf_path


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("inputs", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--url", "urls", multiple=True, help="Invoice download URL. Repeat for multiple CSV or Excel files.")
@click.option(
    "--outdir",
    default="output",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory where CSV outputs, charts and the PDF report are saved.",
)
@click.option("--show-plot", is_flag=True, help="Display the charts in addition to saving them.")
@click.option("--no-forecast", is_flag=True, help="Skip the machine-learning 12-month forecast.")
@click.option(
    "--buffer",
    "budget_buffer",
    type=float,
    default=0.10,
    show_default=True,
    help="Safety buffer added to the forecast for budget/renewal recommendations.",
)
@click.option(
    "--project",
    "project_values",
    multiple=True,
    help="Project filter. Repeat the option or pass comma-separated names. Defaults to all projects.",
)
def main(
    inputs: tuple[Path, ...],
    outdir: Path,
    show_plot: bool,
    no_forecast: bool,
    budget_buffer: float,
    project_values: tuple[str, ...],
    urls: tuple[str, ...],
) -> None:
    """Analyse any number of CSV or Excel invoices (files or folders). Defaults to ./Invoices."""
    if not inputs and not urls:
        inputs = (Path("Invoices"),)

    files = discover_input_files(inputs)
    if not files and not urls:
        raise click.ClickException("No invoice files found in the supplied paths.")

    outdir = outdir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="atlas-invoices-") as temporary:
        try:
            remote_files = asyncio.run(download_invoice_files(urls, Path(temporary))) if urls else []
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise click.ClickException("Unable to download invoice files. Check the URLs and API access.") from exc
        files.extend(remote_files)
        df, cleaning_log = load_cost_data(files)
    cleaning_log.to_csv(outdir / "cleaning_log.csv", index=False)

    projects = normalize_projects(project_values)
    if projects:
        available = set(df["Project Name"].unique())
        unknown = [p for p in projects if p not in available]
        if unknown:
            raise click.BadParameter(f"Unknown project(s): {', '.join(unknown)}", param_hint="--project")
        df = df[df["Project Name"].isin(projects)].copy()

    summaries = build_summary_tables(df)
    eda_path, eda_figures = run_eda(df, cleaning_log, outdir)

    forecast_tables: dict[str, pd.DataFrame] | None = None
    final_metrics: dict[str, object] | None = None
    if not no_forecast:
        forecast_tables, final_metrics = forecast_next_12_months(df, buffer=budget_buffer)

    forecast_by_project = (
        forecast_tables["forecast_12m_by_project"] if forecast_tables is not None else pd.DataFrame()
    )
    final_report = build_final_report(summaries, forecast_by_project, budget_buffer)

    written: list[Path] = []
    tables_to_write = dict(summaries)
    if forecast_tables is not None:
        tables_to_write.update(forecast_tables)
    for name, table in tables_to_write.items():
        path = outdir / f"{name}.csv"
        table.to_csv(path, index=False)
        written.append(path)

    final_path = outdir / "final_cost_report.csv"
    final_report.to_csv(final_path, index=False)

    figures = eda_figures + build_charts(summaries, forecast_tables)
    chart_paths = save_figures(figures, outdir)
    pdf_path = create_pdf_report(outdir, figures, summaries, final_report, forecast_tables)

    if show_plot:
        plt.show()
    plt.close("all")

    click.echo(f"Invoice files processed: {len(files)}")
    click.echo(f"Clean line items: {len(df):,}")
    click.echo(f"Months covered: {df['Month'].nunique()} ({df['Month'].min()} to {df['Month'].max()})")
    click.echo(f"Total spend: {df['Cost'].sum():,.2f} USD")
    click.echo("\nTotal spend by organization:")
    click.echo(
        summaries["total_spend_by_organization"].to_string(index=False, float_format=lambda v: f"{v:,.2f}")
    )
    click.echo("\nTotal spend by project:")
    click.echo(summaries["total_spend_by_project"].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    click.echo("\nTotal spend by service:")
    click.echo(summaries["spend_by_service"].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    if forecast_tables is not None and final_metrics is not None:
        click.echo(
            f"\nBest model: {final_metrics['Best Model']} "
            f"(test RMSE {float(final_metrics['Final Test RMSE']):,.2f})"
        )
        click.echo("\nNext 12 months forecast by project:")
        click.echo(
            forecast_tables["forecast_12m_by_project"].to_string(index=False, float_format=lambda v: f"{v:,.2f}")
        )
        click.echo("\nOverall projection:")
        click.echo(forecast_tables["forecast_overall_projection"].to_string(index=False))
        click.echo("\nNext 12 months scenarios by organization:")
        click.echo(
            forecast_tables["forecast_12m_by_organization"].to_string(
                index=False, float_format=lambda v: f"{v:,.2f}"
            )
        )

    click.echo(f"\nFinal consolidated CSV: {final_path}")
    click.echo(f"EDA report: {eda_path}")
    click.echo(f"PDF report: {pdf_path}")
    click.echo(f"Charts: {len(chart_paths)} PNG files in {outdir / 'charts'}")
    click.echo(f"Other CSV outputs: {len(written)} files in {outdir}")


if __name__ == "__main__":
    main()
