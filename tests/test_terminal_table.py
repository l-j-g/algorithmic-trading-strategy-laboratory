from __future__ import annotations

import unittest

from ats_lab.terminal_table import Alignment, FittedTable, TableColumn


class FittedTableTests(unittest.TestCase):
    def test_header_and_rows_share_fitted_widths_and_numeric_alignment(self) -> None:
        columns = (
            TableColumn("name", "NAME", 12, 6, 1, required=True),
            TableColumn(
                "count", "COUNT", 7, 5, 2,
                alignment=Alignment.RIGHT, required=True,
            ),
            TableColumn("detail", "DETAIL", 20, 8, 9),
        )
        table = FittedTable(columns, width=28)

        header = table.render_header()
        first = table.render_row({"name": "Alpha", "count": 7, "detail": "long detail"})
        second = table.render_row({"name": "Beta", "count": 120, "detail": "other"})

        self.assertLessEqual(len(header), 28)
        self.assertEqual(header.index("COUNT"), first.index("    7"))
        self.assertEqual(header.index("COUNT"), second.index("  120"))
        self.assertEqual(len(header), len(first))
        self.assertEqual(len(first), len(second))

    def test_low_priority_column_drops_before_required_titles(self) -> None:
        columns = (
            TableColumn("name", "NAME", 12, 6, 1, required=True),
            TableColumn("state", "STATE", 12, 6, 2, required=True),
            TableColumn("detail", "DETAIL", 20, 8, 99),
        )
        header = FittedTable(columns, width=16).render_header()

        self.assertIn("NAME", header)
        self.assertIn("STATE", header)
        self.assertNotIn("DETAIL", header)


if __name__ == "__main__":
    unittest.main()
