import unittest
from unittest.mock import MagicMock

from export_command import (
    parse_export_groups_args,
    resolve_export_batch_size,
)
from group_export import entity_matches_command_filter


class ExportCommandTests(unittest.TestCase):
    def test_batch_size_when_less_than_1000(self) -> None:
        args = parse_export_groups_args("/export_groups 8")
        self.assertEqual(args.batch_size, 8)
        self.assertIsNone(args.command_filter)

    def test_group_id_when_not_less_than_1000(self) -> None:
        args = parse_export_groups_args("/export_groups 4215609865")
        self.assertIsNone(args.batch_size)
        self.assertIsNotNone(args.command_filter)
        assert args.command_filter is not None
        self.assertIsNotNone(args.command_filter.group_id_variants)

    def test_name_keyword_when_not_integer(self) -> None:
        args = parse_export_groups_args("/export_groups decent")
        self.assertIsNone(args.batch_size)
        assert args.command_filter is not None
        self.assertEqual(args.command_filter.name_keyword, "decent")

    def test_command_overrides_config_batch(self) -> None:
        config = {"groups_per_checkpoint": 5}
        self.assertEqual(
            resolve_export_batch_size("/export_groups 3", config),
            3,
        )

    def test_keyword_does_not_set_batch_from_config_in_args(self) -> None:
        args = parse_export_groups_args("/export_groups decent")
        self.assertIsNone(args.batch_size)
        self.assertEqual(
            resolve_export_batch_size("/export_groups decent", {"groups_per_checkpoint": 5}),
            5,
        )


class EntityCommandFilterTests(unittest.TestCase):
    def test_name_fuzzy_match(self) -> None:
        entity = MagicMock()
        entity.title = "Decent 技术沟通群"
        entity.username = None
        entity.id = 1
        self.assertTrue(
            entity_matches_command_filter(
                entity,
                name_keyword="decent",
                group_id_variants=None,
            )
        )

    def test_group_id_exact_match(self) -> None:
        entity = MagicMock()
        entity.title = "X"
        entity.username = None
        entity.id = 4215609865
        variants = frozenset({"4215609865", "-1004215609865"})
        self.assertTrue(
            entity_matches_command_filter(
                entity,
                name_keyword=None,
                group_id_variants=variants,
            )
        )


if __name__ == "__main__":
    unittest.main()
