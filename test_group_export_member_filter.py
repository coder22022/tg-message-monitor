import unittest
from unittest.mock import MagicMock

from group_export import MemberExportFilter, should_include_member


def make_user(**kwargs: object) -> MagicMock:
    user = MagicMock()
    user.bot = kwargs.get("bot", False)
    user.deleted = kwargs.get("deleted", False)
    user.scam = kwargs.get("scam", False)
    user.fake = kwargs.get("fake", False)
    user.first_name = kwargs.get("first_name", "Alice")
    user.last_name = kwargs.get("last_name", "")
    user.username = kwargs.get("username", "alice")
    return user


class MemberExportFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filters = MemberExportFilter()

    def test_excludes_bot(self) -> None:
        user = make_user(bot=True)
        self.assertFalse(should_include_member(user, self.filters))

    def test_excludes_deleted_flag(self) -> None:
        user = make_user(deleted=True)
        self.assertFalse(should_include_member(user, self.filters))

    def test_excludes_deleted_account_name(self) -> None:
        user = make_user(first_name="Deleted Account", username=None)
        self.assertFalse(should_include_member(user, self.filters))

    def test_keeps_normal_user(self) -> None:
        user = make_user()
        self.assertTrue(should_include_member(user, self.filters))


if __name__ == "__main__":
    unittest.main()
