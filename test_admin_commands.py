import unittest

from admin_commands import resolve_admin_command


class ResolveAdminCommandTests(unittest.TestCase):
    def test_legacy_multi_word_commands(self) -> None:
        self.assertEqual(resolve_admin_command("/open push"), "open_push")
        self.assertEqual(resolve_admin_command("  /Close   Push  "), "close_push")
        self.assertEqual(resolve_admin_command("/open duty"), "open_duty")

    def test_bot_menu_commands(self) -> None:
        self.assertEqual(resolve_admin_command("/push_on"), "open_push")
        self.assertEqual(resolve_admin_command("/duty_off"), "close_duty")
        self.assertEqual(resolve_admin_command("/status"), "status")

    def test_unknown_command(self) -> None:
        self.assertIsNone(resolve_admin_command("/foo"))


if __name__ == "__main__":
    unittest.main()
