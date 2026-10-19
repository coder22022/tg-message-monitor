import unittest
from unittest.mock import MagicMock

from group_export import passes_group_filter, build_filter_variant_set


class GroupExportFilterTests(unittest.TestCase):
    def test_include_whitelist(self) -> None:
        entity = MagicMock()
        entity.id = 4215609865
        entity.username = "mygroup"
        include = build_filter_variant_set(["mygroup"])
        self.assertTrue(passes_group_filter(entity, include, set()))

    def test_exclude_blocks_group(self) -> None:
        entity = MagicMock()
        entity.id = -1004215609865
        entity.username = None
        exclude = build_filter_variant_set(["4215609865"])
        self.assertFalse(passes_group_filter(entity, set(), exclude))

    def test_empty_include_exports_all_non_excluded(self) -> None:
        entity = MagicMock()
        entity.id = 100
        entity.username = "x"
        self.assertTrue(passes_group_filter(entity, set(), set()))


if __name__ == "__main__":
    unittest.main()
