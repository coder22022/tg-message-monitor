import unittest
from datetime import datetime

from push import PushClient


WORK_TIME = datetime(2026, 8, 18, 14, 0)
OFF_TIME = datetime(2026, 8, 18, 20, 0)


def make_client() -> PushClient:
    return PushClient(
        {
            "enabled": True,
            "push_key": "test-key",
            "work_hours": [
                {"start": "09:30", "end": "11:00"},
                {"start": "13:30", "end": "18:00"},
            ],
        }
    )


class ShouldNotifyGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = make_client()

    def test_listed_mention_always_notifies(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=False,
                in_watch_list=True,
                moment=OFF_TIME,
            )
        )

    def test_listed_reply_always_notifies(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=True,
                in_watch_list=True,
                moment=OFF_TIME,
            )
        )

    def test_listed_plain_message_notifies_during_duty(self) -> None:
        self.client.open_duty()
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=False,
                in_watch_list=True,
                moment=OFF_TIME,
            )
        )

    def test_listed_plain_message_notifies_during_work_hours(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=False,
                in_watch_list=True,
                moment=WORK_TIME,
            )
        )

    def test_listed_plain_message_skipped_outside_work_and_duty(self) -> None:
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=False,
                in_watch_list=True,
                moment=OFF_TIME,
            )
        )

    def test_unlisted_mention_notifies_during_work_hours(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=False,
                in_watch_list=False,
                moment=WORK_TIME,
            )
        )

    def test_unlisted_mention_skipped_during_duty(self) -> None:
        self.client.open_duty()
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=False,
                in_watch_list=False,
                moment=OFF_TIME,
            )
        )

    def test_unlisted_mention_skipped_when_duty_overrides_work_hours(self) -> None:
        self.client.open_duty()
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=True,
                in_watch_list=False,
                moment=WORK_TIME,
            )
        )

    def test_unlisted_mention_skipped_outside_work_and_duty(self) -> None:
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=False,
                in_watch_list=False,
                moment=OFF_TIME,
            )
        )

    def test_unlisted_plain_message_skipped_during_work_hours(self) -> None:
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=False,
                in_watch_list=False,
                moment=WORK_TIME,
            )
        )

    def test_unlisted_reply_notifies_during_work_hours(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=True,
                in_watch_list=False,
                moment=WORK_TIME,
            )
        )


if __name__ == "__main__":
    unittest.main()
