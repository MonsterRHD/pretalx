# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
from decimal import Decimal

import pytest
from django_scopes import scope

from pretalx.submission.domain.review import (
    create_or_update_review,
    recalculate_submission_scores,
    update_review_score,
)
from pretalx.submission.domain.score_generation import (
    commit_score_generation,
    ensure_baseline_generation,
    refresh_review_scores,
    write_candidate,
)
from pretalx.submission.models import ReviewScoreCandidate, ScoreGenerationStatus
from tests.factories import (
    EventFactory,
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    SubmissionFactory,
    TrackFactory,
    UserFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _weighted_setup():
    """G1 freezes weight 1.0; G2 freezes weight 2.0 for the same option."""
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), active=True, is_independent=False
    )
    score = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event)
    with scope(event=event):
        g1 = ensure_baseline_generation(event)
        category.weight = Decimal("2.0")
        category.save()
        g2 = commit_score_generation(event)
    return event, category, score, submission, g1, g2


def _candidate(review, generation):
    return ReviewScoreCandidate.objects.get(generation=generation, review=review)


def test_refresh_writes_confirmed_value_and_pending_candidate():
    event, category, score, submission, g1, g2 = _weighted_setup()
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(score)

    with scope(event=event):
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.score == Decimal("3.0")  # G1: value 3 * weight 1
    assert _candidate(review, g2).value == Decimal("6.0")  # G2: 3 * 2


def test_refresh_handles_review_without_scores():
    event, category, score, submission, g1, g2 = _weighted_setup()
    review = ReviewFactory(submission=submission, score=Decimal(99))

    with scope(event=event):
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.score is None
    assert _candidate(review, g2).value is None
    assert _candidate(review, g2).review_updated == review.updated


def test_score_propagation_does_not_bump_review_timestamp():
    event, category, score, submission, g1, g2 = _weighted_setup()
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(score)
    updated_before = review.updated

    with scope(event=event):
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.updated == updated_before
    assert _candidate(review, g2).review_updated == review.updated


def test_review_edit_during_recalculation_enters_both_generations():
    # score2 exists when G2 is committed but not when G1 was frozen.
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), active=True, is_independent=False
    )
    score = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event)
    user = UserFactory()

    with scope(event=event):
        ensure_baseline_generation(event)
        # G2 changes the weight and introduces a second option.
        category.weight = Decimal("2.0")
        category.save()
        score2 = ReviewScoreFactory(category=category, value=Decimal("5.0"))
        g2 = commit_score_generation(event)

    with scope(event=event):
        review = create_or_update_review(
            submission=submission, user=user, text="v1", scores=[score]
        )
        assert review.score == Decimal("3.0")
        assert _candidate(review, g2).value == Decimal("6.0")

        # Second edit: score2 only exists in G2, so the G1 value drops out.
        review = create_or_update_review(
            submission=submission, user=user, text="v2", scores=[score2]
        )
        assert review.score is None
        candidate = _candidate(review, g2)
        assert candidate.value == Decimal("10.0")
        assert candidate.review_updated == review.updated
        assert ReviewScoreCandidate.objects.filter(generation=g2).count() == 1


def test_abstention_creates_null_pending_candidate():
    event, category, score, submission, g1, g2 = _weighted_setup()
    review = ReviewFactory(submission=submission, score=None)

    with scope(event=event):
        update_review_score(review)

    review.refresh_from_db()
    assert review.score is None
    assert _candidate(review, g2).value is None


def test_track_change_recalculates_both_generations():
    event = EventFactory()
    event.score_categories.all().delete()
    t1 = TrackFactory(event=event, name="T1")
    t2 = TrackFactory(event=event, name="T2")
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), is_independent=False
    )
    category.limit_tracks.add(t1)
    score = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event, track=None)
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(score)

    with scope(event=event):
        ensure_baseline_generation(event)
        category.limit_tracks.set([t2])
        g2 = commit_score_generation(event)

        submission.track = t1
        submission.save()
        recalculate_submission_scores(submission)
        review.refresh_from_db()
        assert review.score == Decimal("3.0")  # G1 allows t1
        assert _candidate(review, g2).value is None  # G2 only allows t2

        submission.track = t2
        submission.save()
        recalculate_submission_scores(submission)
        review.refresh_from_db()
        assert review.score is None  # G1 only allows t1
        assert _candidate(review, g2).value == Decimal("3.0")


def test_write_candidate_noop_for_superseded_generation():
    event, category, score, submission, g1, g2 = _weighted_setup()
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(score)

    with scope(event=event):
        g3 = commit_score_generation(event)  # supersedes g2
        g2.refresh_from_db()
        assert g2.status == ScoreGenerationStatus.SUPERSEDED

        result = write_candidate(review, g2)

    assert result is False
    assert not ReviewScoreCandidate.objects.filter(
        generation=g2, review=review
    ).exists()
    # The fresh pending generation still receives the candidate.
    assert write_candidate(review, g3) is True
    assert _candidate(review, g3).value == Decimal("6.0")


def test_refresh_without_generation_uses_live_rules():
    event = EventFactory()
    category = ReviewScoreCategoryFactory(event=event, weight=Decimal("2.0"))
    score = ReviewScoreFactory(category=category, value=Decimal("5.0"))
    submission = SubmissionFactory(event=event)
    review = ReviewFactory(submission=submission, score=None)
    review.scores.add(score)

    with scope(event=event):
        refresh_review_scores(review)

    review.refresh_from_db()
    assert review.score == Decimal("10.0")
    assert ReviewScoreCandidate.objects.count() == 0
