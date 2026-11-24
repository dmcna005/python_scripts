import asyncio
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
import pandas as pd
from click.testing import CliRunner

import atlas_billing_app
import cost_analysis


class InvoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.first = self.root / "first.csv"
        self.second = self.root / "second.xlsx"
        self.data = pd.DataFrame({
            "Billed Date": ["2026-01-01", "2026-02-01", "2026-03-01"],
            "Project Name": ["First"] * 3,
            "Service": ["Compute"] * 3,
            "Cost": [10, 20, 30],
        })
        self.data.to_csv(self.first, index=False)
        self.data.assign(**{"Project Name": "Second"}).to_excel(self.second, index=False)

    def test_mixed_files_keep_same_month_invoices(self):
        files = cost_analysis.discover_input_files((self.root, self.first))
        data, log = cost_analysis.load_cost_data(files)
        self.assertEqual(len(files), 2)
        self.assertEqual(len(data), 6)
        self.assertEqual(data["Cost"].sum(), 120)
        self.assertEqual(set(log["Stage"]) & {"skipped", "duplicate month dropped"}, set())

    def test_extensionless_csv_still_supported(self):
        extensionless = self.root / "April"
        self.data.to_csv(extensionless, index=False)
        self.assertEqual(len(cost_analysis.load_invoice_file(extensionless)), 3)

    def test_compressed_twins_are_matched_by_full_path(self):
        other = self.root / "other"
        other.mkdir()
        compressed = other / "first.csv.gz"
        self.data.to_csv(compressed, index=False, compression="gzip")
        self.assertEqual(
            cost_analysis.discover_input_files((self.first, compressed)),
            [self.first.resolve(), compressed.resolve()],
        )

    def test_click_generates_reports_from_mixed_inputs(self):
        output = self.root / "reports"
        result = CliRunner().invoke(
            cost_analysis.main,
            [str(self.first), str(self.second), "--no-forecast", "--outdir", str(output)],
        )
        self.assertEqual(result.exit_code, 0, str(result.exception) + result.output)
        self.assertIn("Total spend: 120.00 USD", result.output)
        self.assertTrue((output / "final_cost_report.csv").exists())
        self.assertTrue(list(output.glob("*.pdf")))

    def test_reporting_cli_accepts_positional_and_repeated_options(self):
        for arguments in (
            [str(self.first), str(self.second)],
            ["--csv", str(self.first), "--input", str(self.second)],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(atlas_billing_app, "load_config", return_value=atlas_billing_app.AppConfig()),
                patch.object(atlas_billing_app, "create_pdf_report", return_value=self.root / "report.pdf") as report,
                patch.object(atlas_billing_app, "send_email"),
                patch.object(atlas_billing_app, "load_billing_from_mongo") as mongo,
            ):
                result = CliRunner().invoke(atlas_billing_app.main, [*arguments, "--no-email"])
                self.assertEqual(result.exit_code, 0, str(result.exception) + result.output)
                self.assertEqual(len(report.call_args.args[0]), 6)
                mongo.assert_not_called()

    def test_downloads_are_concurrent_and_support_excel(self):
        async def check():
            active = 0
            maximum = 0
            both_started = asyncio.Event()

            async def handle(request):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=2)
                active -= 1
                content = (
                    self.second.read_bytes()
                    if request.url.path.endswith(".xlsx")
                    else self.first.read_bytes()
                )
                return httpx.Response(200, content=content)

            factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(handle))
            with patch.object(cost_analysis.httpx, "AsyncClient", factory):
                files = await cost_analysis.download_invoice_files(
                    ("https://example.test/first.csv", "https://example.test/second.xlsx"), self.root
                )
            data, _ = cost_analysis.load_cost_data(files)
            self.assertEqual(maximum, 2)
            self.assertEqual(len(data), 6)

        asyncio.run(check())

    def test_http_failure_is_a_click_error(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        factory = partial(httpx.AsyncClient, transport=transport)
        with patch.object(cost_analysis.httpx, "AsyncClient", factory):
            result = CliRunner().invoke(
                cost_analysis.main,
                ["--url", "https://example.test/invoice.csv", "--outdir", str(self.root / "reports")],
            )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Unable to download invoice files", result.output)

    def test_non_http_url_is_rejected(self):
        result = CliRunner().invoke(
            cost_analysis.main,
            ["--url", "file:///invoice.csv", "--outdir", str(self.root / "reports")],
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("HTTP or HTTPS", result.output)


if __name__ == "__main__":
    unittest.main()