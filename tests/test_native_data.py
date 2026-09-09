import unittest
from datetime import datetime
from codex_glass.storage.index import history_page


class NativeHistoryTests(unittest.TestCase):
    def test_time_and_multi_model_filters_apply_before_count_and_pagination(self):
        events = [
            dict(
                timestamp=f"2026-09-08 {h}:00:00", model=m, cwd="work", tokens={"total": n}, cost_usd={"total": n / 100}
            )
            for h, m, n in [("22", "a", 30), ("21", "b", 20), ("20", "c", 10), ("09", "a", 1)]
        ]
        page = history_page(
            events, None, offset=1, limit=1, since="2026-09-08 17:00:00", until="2026-09-08 22:00:00", models=["a", "b"]
        )
        self.assertEqual(2, page["total"])
        self.assertEqual("b", page["events"][0]["model"])

    def test_explicit_empty_selection_returns_no_models(self):
        self.assertEqual(0, history_page([{"model": "a"}], None, models=[])["total"])

    def test_numeric_sort_uses_facts_not_formatted_text(self):
        events = [dict(model="a", tokens={"total": 2}), dict(model="b", tokens={"total": 100})]
        page = history_page(events, None, sort="tokens", descending=True)
        self.assertEqual(["b", "a"], [r["model"] for r in page["events"]])


class NativeProjectionTests(unittest.TestCase):
    def test_export_preserves_numeric_facts_and_quotes_formula_strings(self):
        import csv, tempfile
        from pathlib import Path
        from codex_glass.desktop.data import export_events

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "export.csv"
            export_events(
                path,
                [
                    dict(
                        timestamp="2026-09-08",
                        model="=formula",
                        cwd="@path",
                        tokens={"total": 123},
                        pricing_source="unpriced",
                    )
                ],
            )
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual("'=formula", rows[1][1])
            self.assertEqual("'@path", rows[1][2])
            self.assertEqual("123", rows[1][-2])
            self.assertEqual("未计价", rows[1][-1])

    def test_structure_excludes_cache_double_count_and_reasoning_double_count(self):
        import codex_glass.desktop.data as d

        self.assertEqual(
            [10, 80, 10],
            d.token_structure(
                {"input_tokens": 90, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 5}
            ),
        )

    def test_scope_bounds_and_unknown_cost(self):
        import codex_glass.desktop.data as d

        self.assertEqual(
            ("2026-09-08 17:00:00", "2026-09-08 22:00:00"), d.scope_bounds("last_5_hours", datetime(2026, 9, 8, 22))
        )
        self.assertEqual("未计价", d.cost_text({"estimated_cost_usd": 0}, True))

    def test_model_filter_changes_projection_not_original_facts(self):
        import codex_glass.desktop.data as d

        data = {"total": {"total_tokens": 30}, "by_model": {"a": {"total_tokens": 10}, "b": {"total_tokens": 20}}}
        rows = d.model_rows(data, "all", selected={"b"})
        self.assertEqual(["b"], [r["name"] for r in rows])
        self.assertEqual(30, data["total"]["total_tokens"])
