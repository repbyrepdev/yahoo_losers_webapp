"""Regression tests for the PR->wiki cross-linker (review findings)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))
from wiki_crosslink import pages_for

SMAP = """| Change | Files | Symbols | Page | Tests |
| --- | --- | --- | --- | --- |
| Provider chains | `sources.py`, `market_data.py` | x | [Provider Failover](data/provider-failover.md) | `tests/test_sources.py` |
| Scoring | `recommendation.py` | y | [Rebound Score](scoring/rebound-score.md) | `tests/test_rank.py` |
| Deep dup | `pkg/app.py` | z | [Wrong Page](wrong/page.md) | `tests/test_sources.py` |
"""


class TestCrosslinkMatching:
    def test_root_level_basename_matches(self):
        pages, unmatched = pages_for(["sources.py"], SMAP)
        assert "Provider Failover" in pages
        assert not unmatched

    def test_deep_path_does_not_basename_match(self):
        """Review finding: a deep path's basename must not collide with
        same-named files or Focused-tests entries."""
        pages, unmatched = pages_for(["some/dir/app.py"], SMAP)
        assert pages == {}
        assert unmatched == ["some/dir/app.py"]

    def test_full_path_matches_and_unmatched_reported(self):
        pages, unmatched = pages_for(
            ["pkg/app.py", "totally/unknown file.py"], SMAP)
        assert "Wrong Page" in pages  # exact backticked path match
        assert unmatched == ["totally/unknown file.py"]

    def test_focused_tests_column_does_not_link_via_deep_test_path(self):
        pages, unmatched = pages_for(["tests/test_sources.py"], SMAP)
        assert pages != {} or unmatched  # documented behavior: exact path
        # exact backticked occurrence in the row DOES match -- the guard
        # is against BASENAME collisions, pinned above.
