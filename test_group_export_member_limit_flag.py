import unittest

from group_export import GROUP_CSV_HEADERS


class IncludesAllMembersColumnTests(unittest.TestCase):
    def test_header_contains_includes_all_members(self) -> None:
        self.assertIn("includes_all_members", GROUP_CSV_HEADERS)
        member_count_index = GROUP_CSV_HEADERS.index("member_count")
        includes_index = GROUP_CSV_HEADERS.index("includes_all_members")
        self.assertEqual(includes_index, member_count_index + 1)


if __name__ == "__main__":
    unittest.main()
