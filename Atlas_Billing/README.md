# Atlas Billing CLI

Both entry points use Click. Install dependencies in the project environment:

```sh
./.venv/bin/python -m pip install -r requirements.txt
```

## Invoice Analysis

Pass any number of CSV or Excel files, including mixed formats:

```sh
./.venv/bin/python cost_analysis.py january.csv february.xlsx march.xls --outdir output
./.venv/bin/python cost_analysis.py Invoices --no-forecast
./.venv/bin/python cost_analysis.py --help
```

With no inputs, the analysis reads `./Invoices`. Directory discovery reads immediate
child files, not nested directories. Existing extensionless CSV exports and compressed
CSV files remain supported. Excel formats are `.xlsx`, `.xlsm`, `.xls`, and `.xlsb`;
the first worksheet is read with the same invoice column requirements as CSV.
Distinct invoice rows from the same month are retained across input files.
Forecasting requires at least three billing months; use `--no-forecast` for shorter histories.

Repeat `--url` to download invoice exports concurrently using `httpx.AsyncClient`:

```sh
./.venv/bin/python cost_analysis.py local.csv \
  --url https://example.com/january.csv \
  --url https://example.com/february.xlsx \
  --no-forecast --outdir output
```

URLs must return CSV or Excel bytes, not JSON. Use a URL ending in the Excel filename
for Excel downloads. Only HTTP(S) URLs are accepted. Requests have a 60-second timeout;
HTTP failures stop the command with an error. Downloaded files are temporary and removed
after loading. URL-only runs do not also load `./Invoices`.
There is no Atlas REST API authentication or JSON invoice API integration.

## Configured Reporting

The reporting command also accepts any number of local CSV or Excel files:

```sh
./.venv/bin/python atlas_billing_app.py january.csv february.xlsx --no-email
./.venv/bin/python atlas_billing_app.py --input january.csv --input february.xlsx --no-email
```

The existing `--csv` option remains an alias for `--input` and can be repeated.
Without file inputs, the command uses its configured MongoDB source. Configuration
and email behavior remain controlled by `--config` and `--no-email`.
MongoDB and SMTP are not HTTP requests and do not use HTTPX.

## Tests

```sh
MPLBACKEND=Agg ./.venv/bin/python -m unittest -v test_cost_analysis
```