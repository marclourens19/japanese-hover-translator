"""Pure SM-2 scheduling primitives used by the study dashboard.

The scheduler has no database or UI dependencies. Supplying ``reviewed_at`` makes
every transition deterministic, which keeps migration and unit tests repeatable.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


MIN_EASE_FACTOR = 1.3
DEFAULT_EASE_FACTOR = 2.5
RATING_QUALITY = {
    "again": 1,
    "hard": 3,
    "good": 4,
    "easy": 5,
}


@dataclass(frozen=True)
class ReviewState:
    """A saved word's SM-2 state going into one review -- built from a
    saved_words row's repetitions/interval_days/ease_factor/etc. columns.
    Defaults describe a brand-new, never-reviewed card."""

    repetitions: int = 0
    interval_days: int = 0
    ease_factor: float = DEFAULT_EASE_FACTOR
    due_at: Optional[datetime] = None
    last_reviewed_at: Optional[datetime] = None
    review_count: int = 0
    lapses: int = 0


@dataclass(frozen=True)
class ReviewResult:
    """The new state after schedule_review() applies one rating -- same
    shape as ReviewState plus the rating/quality that produced it, ready to
    write straight back into the saved_words row."""

    repetitions: int
    interval_days: int
    ease_factor: float
    due_at: datetime
    last_reviewed_at: datetime
    review_count: int
    lapses: int
    rating: str
    quality: int


def utc_now():
    """Current time in UTC, truncated to whole seconds (matching the
    precision format_db_datetime stores, so round-tripping through the DB
    never introduces spurious sub-second differences)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_utc(value):
    """Coerce a datetime to UTC -- treats a naive datetime as already UTC
    (that's the only kind this module ever produces or stores) rather than
    the local timezone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_db_datetime(value):
    """Store UTC using SQLite's sortable ``YYYY-MM-DD HH:MM:SS`` format."""
    return normalize_utc(value).strftime("%Y-%m-%d %H:%M:%S")


def parse_db_datetime(value):
    """Inverse of format_db_datetime -- parse a stored string (or NULL/"",
    both of which mean "never reviewed") back into a UTC datetime."""
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return normalize_utc(parsed)


def is_due(due_at, now=None):
    """Whether a card is due: NULL due_at (never reviewed) is always due;
    otherwise due once `now` has reached it. Mirrors the SQL WHERE clauses
    used directly in dashboard_app.py's queries -- kept here too since it's
    the natural place for it and useful wherever Python-side due checks
    are more convenient than another query."""
    if due_at is None:
        return True
    return normalize_utc(due_at) <= normalize_utc(now or utc_now())


def schedule_review(state, rating, reviewed_at=None):
    """Apply one SuperMemo SM-2 review transition.

    Ratings map to the original 0-5 quality scale: Again=1, Hard=3,
    Good=4, Easy=5. A quality below 3 resets repetitions; successful
    intervals are 1 day, 6 days, then the prior interval multiplied by the
    ease factor. Ease never falls below 1.3.
    """
    rating = str(rating).lower()
    if rating not in RATING_QUALITY:
        raise ValueError(f"Unknown review rating: {rating!r}")

    quality = RATING_QUALITY[rating]
    reviewed_at = normalize_utc(reviewed_at or utc_now()).replace(microsecond=0)
    repetitions = max(0, int(state.repetitions))
    previous_interval = max(0, int(state.interval_days))
    previous_ease = max(MIN_EASE_FACTOR, float(state.ease_factor))
    lapses = max(0, int(state.lapses))

    # Quality < 3 ("Again") is a lapse: the card is treated as forgotten, so
    # repetitions resets to 0 and it comes back tomorrow rather than
    # continuing along its previous interval progression.
    if quality < 3:
        repetitions = 0
        interval_days = 1
        lapses += 1
    else:
        # A successful review advances the standard SM-2 interval sequence:
        # 1st success -> 1 day, 2nd success -> 6 days, every success after
        # that -> previous interval scaled by the current ease factor (so
        # intervals grow geometrically the more a card is remembered).
        repetitions += 1
        if repetitions == 1:
            interval_days = 1
        elif repetitions == 2:
            interval_days = 6
        else:
            interval_days = max(1, round(previous_interval * previous_ease))

    # The classic SM-2 ease-factor update formula: a perfect "Easy" (quality
    # 5) leaves ease unchanged (delta 0.1 - 0*... = +0.1, roughly a small
    # bump), while progressively worse ratings shrink it more sharply -- an
    # "Again" (quality 1) pulls ease down hardest. Floored at MIN_EASE_FACTOR
    # so a struggling card's interval still grows a little each success,
    # rather than stalling completely.
    ease_delta = 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    ease_factor = max(MIN_EASE_FACTOR, previous_ease + ease_delta)
    due_at = reviewed_at + timedelta(days=interval_days)

    return ReviewResult(
        repetitions=repetitions,
        interval_days=interval_days,
        ease_factor=round(ease_factor, 4),
        due_at=due_at,
        last_reviewed_at=reviewed_at,
        review_count=max(0, int(state.review_count)) + 1,
        lapses=lapses,
        rating=rating,
        quality=quality,
    )


def stage_label(repetitions, review_count):
    """The Saved words page's New/Learning/Review chip: New = never
    reviewed, Learning = reviewed but hasn't strung together two
    consecutive successful ratings yet, Review = past that point (matches
    schedule_review's 1-day/6-day/ease-multiplied interval progression)."""
    if int(review_count) == 0:
        return "New"
    if int(repetitions) < 2:
        return "Learning"
    return "Review"
