from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from analysis.core import (
    aggregate_player_qb_seasons,
    calculate_win_pct,
    canonical_franchise,
    cluster_bootstrap_ci,
    construct_strict_pairs,
    pearson_correlation,
    spearman_correlation,
)


class CoreTests(unittest.TestCase):
    def test_franchise_mappings_preserve_relocations(self) -> None:
        self.assertEqual(canonical_franchise("STL"), "LAR")
        self.assertEqual(canonical_franchise("LA"), "LAR")
        self.assertEqual(canonical_franchise("SD"), "LAC")
        self.assertEqual(canonical_franchise("OAK"), "LV")
        self.assertEqual(canonical_franchise("WAS"), "WAS")

    def test_player_aggregation_continues_across_teams(self) -> None:
        rows = pd.DataFrame(
            {
                "season": [2021, 2021],
                "player_id": ["QB1", "QB1"],
                "player_name": ["Quarter Back", "Quarter Back"],
                "qb_epa": [10.0, 5.0],
                "dropbacks": [100, 50],
            }
        )
        out = aggregate_player_qb_seasons(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.loc[0, "dropbacks"], 150)
        self.assertAlmostEqual(out.loc[0, "metric"], 0.1)

    def test_strict_windows_never_fill_missing_history(self) -> None:
        rows = pd.DataFrame(
            {
                "season": [2006, 2007, 2008, 2010, 2011],
                "entity": ["A"] * 5,
                "metric": [1, 2, 3, 4, 5],
            }
        )
        pairs = construct_strict_pairs(rows, entity_col="entity", window=2)
        self.assertEqual(pairs[["history_start", "history_end", "target_season"]].values.tolist(), [[2006, 2007, 2008]])
        self.assertEqual(len(construct_strict_pairs(rows, entity_col="entity", window=5)), 0)

    def test_tie_is_half_win(self) -> None:
        self.assertAlmostEqual(calculate_win_pct(8, 7, 1), 0.53125)

    def test_rank_and_linear_correlations(self) -> None:
        self.assertAlmostEqual(pearson_correlation([1, 2, 3], [2, 4, 6]), 1.0)
        self.assertAlmostEqual(spearman_correlation([4, 1, 2, 3], [40, 10, 20, 30]), 1.0)
        self.assertTrue(math.isnan(pearson_correlation([1, 1, 1], [1, 2, 3])))

    def test_cluster_bootstrap_is_seeded(self) -> None:
        pairs = pd.DataFrame(
            {
                "entity": ["A", "A", "B", "B", "C", "C"],
                "predictor": [1, 2, 2, 3, 3, 4],
                "outcome": [1, 3, 2, 4, 3, 5],
            }
        )
        first = cluster_bootstrap_ci(pairs, replicates=300, seed=42)
        second = cluster_bootstrap_ci(pairs, replicates=300, seed=42)
        self.assertTrue(np.allclose(first, second))


if __name__ == "__main__":
    unittest.main()
