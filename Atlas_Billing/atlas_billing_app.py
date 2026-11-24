from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import click
import numpy as np
import pandas as pd
import seaborn as sns
import yaml
from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from pymongo import MongoClient

from cost_analysis import add_time_features, evaluate_models, read_billing_file


@dataclass
class MongoConfig:
    uri: str | None = None
    uri_env: str = "MONGODB_ATLAS_BILLING_URI"
    database: str = "billing-billing"
    line_items_collection: str = "lineItems"
    invoices_collection: str = "invoices"
    org_info_collection: str = "orgInfo"
    query: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailConfig:
    enabled: bool = True
    smtp_host: str = "localhost"
    smtp_port: int = 25
    username: str | None = None
    password_env: str | None = None
    use_tls: bool = False
    use_ssl: bool = False
    sender: str = "atlas-billing@example.com"
    recipients: list[str] = field(default_factory=list)
    subject: str = "MongoDB Atlas billing report"


@dataclass
class ReportConfig:
    output_dir: Path = Path("output_reports")
    report_date: str | None = None
    top_n: int = 15
    renewal_buffer: float = 0.10


@dataclass
class AppConfig:
    mongo: MongoConfig = field(default_factory=MongoConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    report: ReportConfig = field(default_factory=ReportConfig)


def _merge_dataclass(instance: Any, values: dict[str, Any]) -> Any:
    for key, value in values.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: Path) -> AppConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = _merge_dataclass(AppConfig(), raw)
    config.report.output_dir = Path(config.report.output_dir).expanduser()
    return config


def build_billing_pipeline(config: MongoConfig) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = []
    if config.query:
        pipeline.append({"$match": config.query})

    pipeline.extend(
        [
            {
                "$lookup": {
                    "from": config.invoices_collection,
                    "localField": "invoiceId",
                    "foreignField": "id",
                    "as": "invoice",
                }
            },
            {"$unwind": {"path": "$invoice", "preserveNullAndEmptyArrays": True}},
            {
                "$lookup": {
                    "from": config.org_info_collection,
                    "localField": "org.id",
                    "foreignField": "_id",
                    "as": "lineItemOrgInfo",
                }
            },
            {
                "$lookup": {
                    "from": config.org_info_collection,
                    "localField": "invoice.orgId",
                    "foreignField": "_id",
                    "as": "invoiceOrgInfo",
                }
            },
            {
                "$addFields": {
                    "resolvedOrgInfo": {
                        "$ifNull": [
                            {"$first": "$lineItemOrgInfo"},
                            {"$first": "$invoiceOrgInfo"},
                        ]
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "Billed Date": "$date",
                    "Invoice Id": "$invoiceId",
                    "Invoice Start Date": "$invoice.startDate",
                    "Invoice End Date": "$invoice.endDate",
                    "Organization Id": {"$ifNull": ["$org.id", "$invoice.orgId"]},
                    "Organization Name": {
                        "$ifNull": [
                            "$resolvedOrgInfo.name",
                            {"$ifNull": ["$org.name", {"$ifNull": ["$org.id", "$invoice.orgId"]}]},
                        ]
                    },
                    "Project Name": {
                        "$ifNull": [
                            "$project.name",
                            {"$ifNull": ["$project.id", "Unassigned"]},
                        ]
                    },
                    "Project Id": "$project.id",
                    "Cluster Name": {"$ifNull": ["$cluster", "Unassigned"]},
                    "Category": {"$ifNull": ["$category", "Unspecified"]},
                    "Service": {"$ifNull": ["$sku", {"$ifNull": ["$category", "Unspecified"]}]},
                    "Provider": {"$ifNull": ["$provider", "Unspecified"]},
                    "Instance Size": {"$ifNull": ["$instance", "Unspecified"]},
                    "Usage Amount": {
                        "$convert": {
                            "input": "$cost",
                            "to": "double",
                            "onError": 0,
                            "onNull": 0,
                        }
                    },
                    "Quantity": "$quantity",
                    "Unit": "$unit",
                    "Unit Price Dollars": {
                        "$convert": {
                            "input": "$unitPriceDollars",
                            "to": "double",
                            "onError": 0,
                            "onNull": 0,
                        }
                    },
                }
            },
        ]
    )
    return pipeline


def load_billing_from_mongo(config: MongoConfig) -> pd.DataFrame:
    uri = config.uri or os.getenv(config.uri_env)
    if not uri:
        raise ValueError(
            f"MongoDB URI is not configured. Set mongo.uri or the {config.uri_env} environment variable."
        )

    with MongoClient(uri) as client:
        collection = client[config.database][config.line_items_collection]
        documents = list(collection.aggregate(build_billing_pipeline(config), allowDiskUse=True))

    if not documents:
        raise ValueError("MongoDB query returned no billing documents.")
    return pd.DataFrame(documents)


def _first_present(row: pd.Series, candidates: list[str], default: Any = None) -> Any:
    for candidate in candidates:
        value = row.get(candidate)
        if pd.notna(value) and value != "":
            return value
    return default


def _jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_instance_size(row: pd.Series) -> str:
    for source_col in ["Instance Size", "instanceSize", "x_InstanceSize"]:
        value = row.get(source_col)
        if pd.notna(value) and value != "":
            return str(value)

    details = _jsonish(row.get("SkuPriceDetails"))
    tags = _jsonish(row.get("Tags"))
    for source in [details, tags]:
        for key in ["instanceSize", "instance_size", "INSTANCE_SIZE", "clusterTier", "tier"]:
            value = source.get(key)
            if value:
                return str(value)

    sku_meter = row.get("SkuMeter")
    if pd.notna(sku_meter) and sku_meter:
        return str(sku_meter)
    return "Unspecified"


def normalize_billing_data(raw_df: pd.DataFrame) -> pd.DataFrame:
    if raw_df.empty:
        raise ValueError("No billing rows were provided.")

    df = raw_df.copy()
    df.columns = [str(col).strip() for col in df.columns]

    normalized = pd.DataFrame()
    normalized["Billed Date"] = df.apply(
        lambda row: _first_present(
            row,
            ["Billed Date", "date", "x_BilledDateStart", "ChargePeriodStart", "BillingPeriodStart"],
        ),
        axis=1,
    )
    normalized["Project Name"] = df.apply(
        lambda row: _first_present(row, ["Project Name", "project.name", "projectName", "SubAccountName"], "Unassigned"),
        axis=1,
    )
    normalized["Cluster Name"] = df.apply(
        lambda row: _first_present(row, ["Cluster Name", "cluster", "ResourceName", "clusterName"], "Unassigned"),
        axis=1,
    )
    normalized["Service"] = df.apply(
        lambda row: _first_present(row, ["Service", "sku", "ServiceName", "ChargeDescription"], "Unspecified"),
        axis=1,
    )
    normalized["Category"] = df.apply(
        lambda row: _first_present(row, ["Category", "category", "x_SkuCategory", "ServiceCategory", "ChargeCategory"], "Unspecified"),
        axis=1,
    )
    normalized["Instance Size"] = df.apply(_extract_instance_size, axis=1)
    normalized["Usage Amount"] = df.apply(
        lambda row: _first_present(row, ["Usage Amount", "cost", "EffectiveCost", "BilledCost", "ContractedCost"], 0.0),
        axis=1,
    )

    normalized["Billed Date"] = pd.to_datetime(normalized["Billed Date"], errors="coerce", utc=True).dt.tz_localize(None)
    normalized["Usage Amount"] = pd.to_numeric(normalized["Usage Amount"], errors="coerce")
    normalized["Usage Amount"] = np.where(np.isfinite(normalized["Usage Amount"]), normalized["Usage Amount"], 0.0)
    normalized = normalized.dropna(subset=["Billed Date"])

    if normalized.empty:
        raise ValueError("No rows had a usable billed date after normalization.")

    normalized["Month"] = normalized["Billed Date"].dt.to_period("M").astype(str)
    normalized["Month Start"] = normalized["Billed Date"].dt.to_period("M").dt.to_timestamp()
    for col in ["Project Name", "Cluster Name", "Service", "Category", "Instance Size"]:
        normalized[col] = normalized[col].fillna("Unspecified").astype(str).replace("", "Unspecified")
    return normalized


def _period_masks(df: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, pd.Series]:
    month_start = as_of.replace(day=1)
    last_month_start = month_start - pd.DateOffset(months=1)
    year_start = pd.Timestamp(year=as_of.year, month=1, day=1)
    return {
        "mtd": (df["Billed Date"] >= month_start) & (df["Billed Date"] <= as_of),
        "last_month": (df["Billed Date"] >= last_month_start) & (df["Billed Date"] < month_start),
        "ytd": (df["Billed Date"] >= year_start) & (df["Billed Date"] <= as_of),
    }


def _money_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_formatter(lambda value, _: f"${value:,.0f}")
    ax.grid(axis="x", linestyle="--", alpha=0.35)


def _add_empty_page(pdf: PdfPages, title: str) -> None:
    fig, ax = plt.subplots(figsize=(11.69, 8.27))
    ax.axis("off")
    ax.set_title(f"{title} (No Data)", fontsize=16, pad=24)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _add_horizontal_bar(pdf: PdfPages, df: pd.DataFrame, group_col: str, title: str, top_n: int) -> None:
    summary = (
        df.groupby(group_col, as_index=False)["Usage Amount"]
        .sum()
        .sort_values("Usage Amount", ascending=False)
        .head(top_n)
    )
    if summary.empty:
        _add_empty_page(pdf, title)
        return

    fig, ax = plt.subplots(figsize=(11.69, 8.27))
    sns.barplot(data=summary, x="Usage Amount", y=group_col, hue=group_col, legend=False, ax=ax)
    ax.set_title(title)
    ax.set_xlabel("Spend")
    ax.set_ylabel(group_col)
    _money_axis(ax)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _add_monthly_bar(pdf: PdfPages, df: pd.DataFrame, category_col: str, title: str, top_n: int) -> None:
    if df.empty:
        _add_empty_page(pdf, title)
        return

    top_values = df.groupby(category_col)["Usage Amount"].sum().nlargest(top_n).index
    chart_df = df[df[category_col].isin(top_values)]
    summary = chart_df.groupby(["Month", category_col], as_index=False)["Usage Amount"].sum()

    fig, ax = plt.subplots(figsize=(13, 8))
    sns.barplot(data=summary, x="Month", y="Usage Amount", hue=category_col, ax=ax)
    ax.set_title(title)
    ax.set_xlabel("Month")
    ax.set_ylabel("Spend")
    ax.tick_params(axis="x", rotation=45)
    ax.yaxis.set_major_formatter(lambda value, _: f"${value:,.0f}")
    ax.legend(title=category_col, bbox_to_anchor=(1.02, 1), loc="upper left")
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _add_combined_ytd_bar(pdf: PdfPages, df: pd.DataFrame, title: str, top_n: int) -> None:
    if df.empty:
        _add_empty_page(pdf, title)
        return

    chart_df = df.copy()
    chart_df["Project / Category / Instance Size"] = (
        chart_df["Project Name"] + " / " + chart_df["Category"] + " / " + chart_df["Instance Size"]
    )
    _add_horizontal_bar(pdf, chart_df, "Project / Category / Instance Size", title, top_n)


def _add_pie(pdf: PdfPages, df: pd.DataFrame, group_col: str, title: str, top_n: int) -> None:
    summary = df.groupby(group_col, as_index=False)["Usage Amount"].sum().sort_values("Usage Amount", ascending=False)
    if summary.empty:
        _add_empty_page(pdf, title)
        return

    if len(summary) > top_n:
        top = summary.head(top_n).copy()
        other = pd.DataFrame([{group_col: "Other", "Usage Amount": summary.iloc[top_n:]["Usage Amount"].sum()}])
        summary = pd.concat([top, other], ignore_index=True)

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.pie(summary["Usage Amount"], labels=summary[group_col], autopct="%1.1f%%", startangle=90)
    ax.set_title(title)
    ax.axis("equal")
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def forecast_next_12_months(df: pd.DataFrame, renewal_buffer: float) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    latest_date = df["Billed Date"].max()
    current_year_data = df[df["Billed Date"].dt.year == latest_date.year].copy()
    monthly_project = (
        current_year_data.groupby(["Month Start", "Project Name"], as_index=False)["Usage Amount"]
        .sum()
        .rename(columns={"Usage Amount": "Credits"})
    )
    projects = sorted(current_year_data["Project Name"].dropna().unique().tolist())
    future_months = pd.date_range(start=(latest_date.to_period("M") + 1).to_timestamp(), periods=12, freq="MS")

    method = "Average monthly YTD consumption"
    future_rows: list[dict[str, Any]] = []
    if monthly_project["Month Start"].nunique() >= 3:
        model_data, origin_month = add_time_features(monthly_project, month_col="Month Start")
        pipeline, _, _, _ = evaluate_models(
            data=model_data,
            target_col="Credits",
            cat_cols=["Project Name"],
            num_cols=["Month Number", "Month Index"],
            time_col="Month Start",
        )
        for month_start in future_months:
            for project_name in projects:
                future_rows.append({"Month Start": month_start, "Project Name": project_name})
        future = pd.DataFrame(future_rows)
        future, _ = add_time_features(future, month_col="Month Start")
        future["Month Index"] = (
            (future["Month Start"].dt.year - origin_month.year) * 12
            + (future["Month Start"].dt.month - origin_month.month)
        )
        future["Predicted Credits"] = np.clip(
            pipeline.predict(future[["Project Name", "Month Number", "Month Index"]]),
            0,
            None,
        )
        method = "ExtraTrees/Ridge/RandomForest model selection from current script"
    else:
        averages = monthly_project.groupby("Project Name", as_index=False)["Credits"].mean()
        for month_start in future_months:
            for row in averages.itertuples(index=False):
                future_rows.append(
                    {
                        "Month Start": month_start,
                        "Project Name": row._asdict()["Project Name"],
                        "Predicted Credits": row.Credits,
                    }
                )
        future = pd.DataFrame(future_rows)

    ytd_by_project = (
        current_year_data.groupby("Project Name", as_index=False)["Usage Amount"]
        .sum()
        .rename(columns={"Usage Amount": "YTD Credits"})
    )
    predicted_by_project = future.groupby("Project Name", as_index=False)["Predicted Credits"].sum()
    summary = ytd_by_project.merge(predicted_by_project, on="Project Name", how="left").fillna(0.0)
    summary["Projected 12-Month Credits"] = summary["Predicted Credits"]
    summary["Recommended Renewal Credits"] = (summary["YTD Credits"] + summary["Projected 12-Month Credits"]) * (
        1 + renewal_buffer
    )
    summary = summary.sort_values("Recommended Renewal Credits", ascending=False)
    return future, summary, method


def _add_forecast_pages(
    pdf: PdfPages,
    future: pd.DataFrame,
    summary: pd.DataFrame,
    method: str,
    top_n: int,
) -> None:
    chart_df = future.copy()
    chart_df["Month"] = chart_df["Month Start"].dt.to_period("M").astype(str)
    chart_df = chart_df.rename(columns={"Predicted Credits": "Usage Amount"})
    _add_monthly_bar(pdf, chart_df, "Project Name", f"Predicted Next 12 Months by Project ({method})", top_n)

    fig, ax = plt.subplots(figsize=(11.69, 8.27))
    table_df = summary.copy()
    for col in table_df.columns:
        if pd.api.types.is_numeric_dtype(table_df[col]):
            table_df[col] = table_df[col].map(lambda value: f"{value:,.2f}")
    ax.axis("off")
    ax.set_title("Project Renewal Forecast Summary", fontsize=16, pad=24)
    table = ax.table(cellText=table_df.head(25).values, colLabels=table_df.columns, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.25)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def create_pdf_report(df: pd.DataFrame, config: ReportConfig) -> Path:
    sns.set_theme(style="whitegrid")
    output_dir = config.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    as_of = pd.Timestamp(config.report_date) if config.report_date else df["Billed Date"].max().normalize()
    masks = _period_masks(df, as_of)
    report_path = output_dir / f"atlas_billing_report_{as_of.date().isoformat()}.pdf"

    future, forecast_summary, forecast_method = forecast_next_12_months(df, config.renewal_buffer)
    future.to_csv(output_dir / "prediction_next_12_months_by_project.csv", index=False)
    forecast_summary.to_csv(output_dir / "prediction_summary_by_project.csv", index=False)

    with PdfPages(report_path) as pdf:
        _add_horizontal_bar(pdf, df[masks["mtd"]], "Project Name", "Biggest Spenders Month to Date", config.top_n)
        _add_horizontal_bar(pdf, df[masks["last_month"]], "Project Name", "Biggest Spenders Last Month", config.top_n)
        _add_horizontal_bar(pdf, df[masks["last_month"]], "Instance Size", "Spend / Instance Size Previous Month", config.top_n)
        _add_monthly_bar(pdf, df, "Project Name", "Spend / Month by Project", config.top_n)
        _add_horizontal_bar(pdf, df[masks["mtd"]], "Category", "Spend by Category Month to Date", config.top_n)
        _add_combined_ytd_bar(pdf, df[masks["ytd"]], "Spend Year to Date by Project, Category, Instance Size", config.top_n)
        _add_monthly_bar(pdf, df, "Cluster Name", "Spend by Month by Clusters", config.top_n)
        _add_horizontal_bar(pdf, df[masks["ytd"]], "Cluster Name", "Spend by Clusters Year to Date", config.top_n)
        _add_pie(pdf, df[masks["last_month"]], "Category", "Total Cost / Category Last Month", config.top_n)
        _add_pie(pdf, df[masks["ytd"]], "Category", "Total Cost / Category Year to Date", config.top_n)
        _add_forecast_pages(pdf, future, forecast_summary, forecast_method, config.top_n)

    return report_path


def send_email(config: EmailConfig, report_path: Path) -> None:
    if not config.enabled:
        return
    if not config.recipients:
        raise ValueError("Email is enabled but no recipients are configured.")

    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = config.subject
    message.set_content("Attached is the MongoDB Atlas billing PDF report.")
    message.add_attachment(
        report_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=report_path.name,
    )

    password = os.getenv(config.password_env) if config.password_env else None
    smtp_class = smtplib.SMTP_SSL if config.use_ssl else smtplib.SMTP
    with smtp_class(config.smtp_host, config.smtp_port) as smtp:
        if config.use_tls and not config.use_ssl:
            smtp.starttls()
        if config.username:
            smtp.login(config.username, password or "")
        smtp.send_message(message)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("inputs", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--config",
    "config_path",
    default=Path("config.yaml"),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to YAML configuration file.",
)
@click.option(
    "--csv",
    "--input",
    "input_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="CSV or Excel input instead of MongoDB. Repeat for multiple files.",
)
@click.option("--no-email", is_flag=True, help="Create the PDF without sending email.")
def main(config_path: Path, input_paths: tuple[Path, ...], no_email: bool, inputs: tuple[Path, ...]) -> None:
    """Generate a billing report from CSV/Excel INPUTS or the configured MongoDB source."""
    config = load_config(config_path.expanduser())
    if no_email:
        config.email.enabled = False

    files = tuple(dict.fromkeys(path.expanduser().resolve() for path in (*inputs, *input_paths)))
    try:
        raw_df = (
            pd.concat([read_billing_file(file) for file in files], ignore_index=True)
            if files
            else load_billing_from_mongo(config.mongo)
        )
    except (ValueError, OSError, ImportError) as exc:
        raise click.ClickException(f"Unable to load billing data: {exc}") from exc
    df = normalize_billing_data(raw_df)
    report_path = create_pdf_report(df, config.report)
    send_email(config.email, report_path)

    print(f"Rows processed: {len(df)}")
    print(f"PDF report: {report_path}")
    if config.email.enabled:
        print(f"Email sent to: {', '.join(config.email.recipients)}")


if __name__ == "__main__":
    main()