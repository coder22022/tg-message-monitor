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

    def test_skip_mention_group_drops_mention_during_work_hours(self) -> None:
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=False,
                in_watch_list=True,
                moment=WORK_TIME,
                skip_self_mention=True,
            )
        )

    def test_skip_mention_group_drops_mention_during_duty(self) -> None:
        self.client.open_duty()
        self.assertFalse(
            self.client.should_notify_group(
                mentioned=True,
                replied_to_me=False,
                in_watch_list=True,
                moment=OFF_TIME,
                skip_self_mention=True,
            )
        )

    def test_skip_mention_group_keeps_plain_message_during_work_hours(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=False,
                in_watch_list=True,
                moment=WORK_TIME,
                skip_self_mention=True,
            )
        )

    def test_skip_mention_group_keeps_reply_outside_work_hours(self) -> None:
        self.assertTrue(
            self.client.should_notify_group(
                mentioned=False,
                replied_to_me=True,
                in_watch_list=True,
                moment=OFF_TIME,
                skip_self_mention=True,
            )
        )


class GroupBroadcastRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = PushClient(
            {
                "enabled": True,
                "push_key": "test-key",
                "group_broadcast_push_limit_per_minute": 2,
            }
        )
        self.now = datetime(2026, 8, 18, 14, 0, 0)

    def test_same_group_hits_limit(self) -> None:
        self.client.record_group_broadcast_push("group-a", self.now)
        self.client.record_group_broadcast_push("group-a", self.now)
        self.assertTrue(
            self.client.is_group_broadcast_rate_limited("group-a", self.now)
        )

    def test_other_group_is_not_limited(self) -> None:
        self.client.record_group_broadcast_push("group-a", self.now)
        self.client.record_group_broadcast_push("group-a", self.now)
        self.assertFalse(
            self.client.is_group_broadcast_rate_limited("group-b", self.now)
        )

    def test_limit_resets_after_one_minute(self) -> None:
        self.client.record_group_broadcast_push("group-a", self.now)
        self.client.record_group_broadcast_push("group-a", self.now)
        later = datetime(2026, 8, 18, 14, 1, 1)
        self.assertFalse(self.client.is_group_broadcast_rate_limited("group-a", later))

    def test_zero_limit_disables_rate_limit(self) -> None:
        client = PushClient(
            {
                "enabled": True,
                "push_key": "test-key",
                "group_broadcast_push_limit_per_minute": 0,
            }
        )
        client.record_group_broadcast_push("group-a", self.now)
        self.assertFalse(client.is_group_broadcast_rate_limited("group-a", self.now))


if __name__ == "__main__":
    unittest.main()

