import json
import unittest
from pathlib import Path

import pandas as pd

from jmeter_compat import build_report_chart_data
from jmeter_parser import parse_jmeter_csv


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "apache_jmeter"
CSV_FILE = FIXTURE_DIR / "HTMLReportTestFile.csv"
EXPECTED_FILE = FIXTURE_DIR / "HTMLReportExpect.json"


class ApacheJMeterStatisticsParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with EXPECTED_FILE.open("r", encoding="utf-8") as handle:
            cls.expected = json.load(handle)

        cls.summary, _ = parse_jmeter_csv(
            str(CSV_FILE),
            green_sla=9999.0,
            amber_sla=10000.0,
            rag_basis="avg",
        )
        cls.by_transaction = {
            row["Transaction"]: row
            for row in cls.summary
        }

    def assert_transaction_matches_jmeter(self, label):
        actual = self.by_transaction[label]
        expected = self.expected[label]

        self.assertEqual(actual["#Samples"], expected["sampleCount"])
        self.assertAlmostEqual(
            actual["Avg (s)"] * 1000.0,
            expected["meanResTime"],
            places=9,
        )
        self.assertAlmostEqual(
            actual["90th % (s)"] * 1000.0,
            expected["pct1ResTime"],
            places=9,
        )
        self.assertAlmostEqual(
            actual["95th % (s)"] * 1000.0,
            expected["pct2ResTime"],
            places=9,
        )
        self.assertAlmostEqual(
            actual["Error %"],
            expected["errorPct"],
            places=6,
        )

    def test_jr_ok_matches_official_jmeter_statistics(self):
        self.assert_transaction_matches_jmeter("JR-OK")

    def test_jr_ko_matches_official_jmeter_statistics(self):
        self.assert_transaction_matches_jmeter("JR-KO")

    def test_display_precision_matches_jmeter_statistics_table(self):
        row = self.by_transaction["JR-OK"]
        expected = self.expected["JR-OK"]

        self.assertEqual(
            f'{row["Avg (s)"] * 1000.0:.2f}',
            f'{expected["meanResTime"]:.2f}',
        )
        self.assertEqual(
            f'{row["90th % (s)"] * 1000.0:.2f}',
            f'{expected["pct1ResTime"]:.2f}',
        )
        self.assertEqual(
            f'{row["95th % (s)"] * 1000.0:.2f}',
            f'{expected["pct2ResTime"]:.2f}',
        )


class ApacheJMeterGraphParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frame = pd.read_csv(CSV_FILE)
        frame.columns = [str(column).strip().lower() for column in frame.columns]

        summary, _ = parse_jmeter_csv(
            str(CSV_FILE),
            green_sla=9999.0,
            amber_sla=10000.0,
            rag_basis="avg",
        )
        cls.chart_data = build_report_chart_data(frame, summary)

    def test_average_response_time_uses_jmeter_end_time_buckets(self):
        self.assertEqual(
            self.chart_data["series_avg_by_txn"]["JR-OK"],
            [236.1029, 235.6638],
        )
        self.assertEqual(
            self.chart_data["series_avg_by_txn"]["JR-KO"],
            [249.5, 100.0],
        )

    def test_response_percentiles_over_time_match_jmeter_semantics(self):
        expected = {
            "Min": [106.0, 101.0],
            "Max": [353.0, 350.0],
            "Median": [227.0, 234.5],
            "90th percentile": [338.3, 333.6],
            "95th percentile": [348.45, 337.3],
            "99th percentile": [353.0, 348.13],
        }
        self.assertEqual(
            self.chart_data["series_response_percentiles_over_time"],
            expected,
        )

    def test_transactions_per_second_split_success_and_failure(self):
        expected = {
            "JR-OK-success": [2.266667, 1.933333],
            "JR-OK-failure": [0.0, 0.0],
            "JR-KO-success": [0.0, 0.0],
            "JR-KO-failure": [0.033333, 0.016667],
        }
        for series_name, values in expected.items():
            self.assertEqual(
                self.chart_data["series_tps_by_txn"][series_name],
                values,
            )


if __name__ == "__main__":
    unittest.main()
