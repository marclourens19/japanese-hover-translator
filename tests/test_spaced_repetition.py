from datetime import datetime, timedelta, timezone
import unittest

from spaced_repetition import (
    ReviewState,
    format_db_datetime,
    is_due,
    parse_db_datetime,
    schedule_review,
    stage_label,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


class SpacedRepetitionTests(unittest.TestCase):
    def state(self, repetitions=0, interval_days=0, ease_factor=2.5, lapses=0):
        return ReviewState(
            repetitions=repetitions,
            interval_days=interval_days,
            ease_factor=ease_factor,
            lapses=lapses,
        )

    @staticmethod
    def next_state(result):
        return ReviewState(
            repetitions=result.repetitions,
            interval_days=result.interval_days,
            ease_factor=result.ease_factor,
            due_at=result.due_at,
            last_reviewed_at=result.last_reviewed_at,
            review_count=result.review_count,
            lapses=result.lapses,
        )

    def test_good_progresses_one_day_six_days_then_ease_interval(self):
        first = schedule_review(self.state(), "good", NOW)
        second = schedule_review(self.next_state(first), "good", NOW)
        third = schedule_review(self.next_state(second), "good", NOW)
        self.assertEqual(first.interval_days, 1)
        self.assertEqual(second.interval_days, 6)
        self.assertEqual(third.interval_days, 15)

    def test_again_resets_repetitions_and_counts_lapse(self):
        result = schedule_review(self.state(4, 30, 2.6, 1), "again", NOW)
        self.assertEqual(result.repetitions, 0)
        self.assertEqual(result.interval_days, 1)
        self.assertEqual(result.lapses, 2)

    def test_ease_never_falls_below_floor(self):
        state = self.state(8, 120, 1.3)
        for _ in range(8):
            state = self.next_state(schedule_review(state, "again", NOW))
        self.assertEqual(state.ease_factor, 1.3)

    def test_due_date_and_database_timestamp_are_stable(self):
        result = schedule_review(self.state(), "easy", NOW)
        encoded = format_db_datetime(result.due_at)
        decoded = parse_db_datetime(encoded)
        self.assertEqual(decoded, result.due_at)
        self.assertFalse(is_due(decoded, NOW))
        self.assertTrue(is_due(decoded, NOW + timedelta(days=2)))

    def test_stages(self):
        self.assertEqual(stage_label(0, 0), "New")
        self.assertEqual(stage_label(1, 1), "Learning")
        self.assertEqual(stage_label(2, 2), "Review")

    def test_invalid_rating_is_rejected(self):
        with self.assertRaises(ValueError):
            schedule_review(self.state(), "perfect", NOW)


if __name__ == "__main__":
    unittest.main()
