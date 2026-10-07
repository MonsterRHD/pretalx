# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
"""Regression tests for review findings R1 (F-1/F-2/F-3)."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django_scopes import scope

from pretalx.submission.domain import score_generation as sg
from pretalx.submission.domain.review import create_or_update_review
from pretalx.submission.domain.score_generation import (
    commit_score_generation,
    ensure_baseline_generation,
    refresh_review_scores,
    run_generation,
)
from pretalx.submission.models import ScoreGenerationStatus
from tests.factories import (
    EventFactory,
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    SubmissionFactory,
    UserFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def test_independent_only_review_keeps_historical_zero_total():
    """F-2: independent options participate with weight 0, so an
    independent-only review stays scored (0) just like the legacy formula."""
    event = EventFactory()
    event.score_categories.all().delete()
    independent = ReviewScoreCategoryFactory(
        event=event, active=True, is_independent=True
    )
    option = ReviewScoreFactory(category=independent, value=Decimal("4.0"))
    submission = SubmissionFactory(event=event)
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(option)

    with scope(event=event):
        ensure_baseline_generation(event)
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.score == Decimal(0)


def test_independent_option_adds_zero_alongside_regular_score():
    event = EventFactory()
    event.score_categories.all().delete()
    regular = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("2.0"), is_independent=False
    )
    independent = ReviewScoreCategoryFactory(event=event, is_independent=True)
    regular_option = ReviewScoreFactory(category=regular, value=Decimal("3.0"))
    independent_option = ReviewScoreFactory(category=independent, value=Decimal("5.0"))
    submission = SubmissionFactory(event=event)
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(regular_option, independent_option)

    with scope(event=event):
        ensure_baseline_generation(event)
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.score == Decimal("6.0")  # 3*2 + 5*0


def test_inactive_independent_option_does_not_count_as_scored():
    event = EventFactory()
    event.score_categories.all().delete()
    independent = ReviewScoreCategoryFactory(
        event=event, active=False, is_independent=True
    )
    option = ReviewScoreFactory(category=independent, value=Decimal("4.0"))
    submission = SubmissionFactory(event=event)
    review = ReviewFactory(submission=submission, score=Decimal(9))
    review.scores.add(option)

    with scope(event=event):
        ensure_baseline_generation(event)
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.score is None  # inactive category never applied, even at 0


def test_new_review_after_confirmation_immediately_uses_new_rules():
    """F-3: a review created after G2 committed must score under G2 even
    though G2's worker never processed it."""
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), is_independent=False
    )
    option = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event)

    with scope(event=event):
        ensure_baseline_generation(event)
        category.weight = Decimal("2.0")
        category.save()
        g2 = commit_score_generation(event)
        assert run_generation(g2.id) == "confirmed"
        g2.refresh_from_db()
        assert g2.status == ScoreGenerationStatus.CONFIRMED

        # Reviewer submits after the switch: live selections + confirmed
        # snapshot must agree immediately, no stale G1 value.
        review = create_or_update_review(
            submission=submission,
            user=UserFactory(),
            text="late review",
            scores=[option],
        )

    assert review.score == Decimal("6.0")
    review.refresh_from_db()
    assert review.score == Decimal("6.0")


def test_refresh_retries_once_after_postgres_deadlock():
    event = EventFactory()
    category = ReviewScoreCategoryFactory(event=event, weight=Decimal("1.0"))
    option = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event)
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(option)

    deadlock = OperationalError("deadlock detected")
    deadlock.orig = type("PgError", (), {"pgcode": "40P01"})()

    with (
        scope(event=event),
        patch.object(
            sg, "_refresh_once", side_effect=[deadlock, review]
        ) as refresh_once,
        patch.object(sg.time, "sleep") as sleep,
    ):
        result = sg.refresh_review_scores(review)

    assert result is review
    assert refresh_once.call_count == 2
    sleep.assert_called_once()


def test_refresh_does_not_retry_non_deadlock_errors():
    event = EventFactory()
    review = ReviewFactory(submission__event=event)
    error = OperationalError("connection reset")
    error.orig = type("PgError", (), {"pgcode": "08006"})()

    with (
        scope(event=event),
        patch.object(sg, "_refresh_once", side_effect=error) as refresh_once,
        pytest.raises(OperationalError),
    ):
        sg.refresh_review_scores(review)

    assert refresh_once.call_count == 1


def test_new_option_included_in_committed_generation_scores_immediately():
    """F-1 domain half: once a generation contains a new option, reviews
    selecting it get the value as a pending candidate and on confirmation."""
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), is_independent=False
    )
    old_option = ReviewScoreFactory(category=category, value=Decimal("1.0"))
    submission = SubmissionFactory(event=event)

    with scope(event=event):
        ensure_baseline_generation(event)
        # Organiser adds a score option in the new settings commit.
        new_option = ReviewScoreFactory(category=category, value=Decimal("7.0"))
        g2 = commit_score_generation(event)

        review = create_or_update_review(
            submission=submission,
            user=UserFactory(),
            text="new option review",
            scores=[new_option],
        )
        # Confirmed G1 never knew this option: no premature contribution.
        assert review.score is None
        candidate = review.score_candidates.get(generation=g2)
        assert candidate.value == Decimal("7.0")  # G2 knows it

        assert run_generation(g2.id) == "confirmed"

    review.refresh_from_db()
    assert review.score == Decimal("7.0")
    assert old_option.pk  # fixture sanity
