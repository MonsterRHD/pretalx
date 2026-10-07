# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_scopes import scope

from pretalx.submission.domain import score_generation as sg
from pretalx.submission.domain.score_generation import (
    confirm_generation,
    generation_is_complete,
    process_generation_batch,
    run_generation,
    sweep_generation,
)
from pretalx.submission.models import ReviewScoreCandidate, ScoreGenerationStatus
from tests.factories import (
    EventFactory,
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    SubmissionFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _setup_event(*, review_count=3, weight_g1="1.0", weight_g2="2.0"):
    """G1 freezes weight 1; G2 freezes weight 2. Reviews hold option value 3."""
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal(weight_g1), active=True, is_independent=False
    )
    option = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event)
    reviews = []
    for _ in range(review_count):
        review = ReviewFactory(submission=submission, score=None)
        review.scores.add(option)
        reviews.append(review)
    abstention = ReviewFactory(submission=submission, score=None)

    with scope(event=event):
        g1 = sg.ensure_baseline_generation(event)
        for review in [*reviews, abstention]:
            sg.refresh_review_scores(review)
        category.weight = Decimal(weight_g2)
        category.save()
        g2 = sg.commit_score_generation(event)

    return event, category, option, reviews, abstention, g1, g2


def test_run_generation_confirms_and_publishes_candidate_values():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()

    with scope(event=event):
        outcome = run_generation(g2.id)

    assert outcome == "confirmed"
    g2.refresh_from_db()
    assert g2.status == ScoreGenerationStatus.CONFIRMED
    assert g2.confirmed_at is not None
    assert g2.cursor_position == max(review.pk for review in [*reviews, abstention])
    for review in reviews:
        review.refresh_from_db()
        assert review.score == Decimal("6.0")  # 3 * weight 2
    abstention.refresh_from_db()
    assert abstention.score is None
    g1.refresh_from_db()
    assert g1.status == ScoreGenerationStatus.SUPERSEDED
    # Candidates of other generations were GC'd; confirmed candidates remain.
    assert (
        ReviewScoreCandidate.objects.filter(generation__event=event).count()
        == len(reviews) + 1
    )


def test_worker_resumes_without_recomputing_existing_candidates():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()
    target = reviews[0]

    with scope(event=event):
        # Simulate a finished first batch that died afterwards.
        assert process_generation_batch(g2) == "progress"
        written = ReviewScoreCandidate.objects.get(generation=g2, review=target)
        assert written.value == Decimal("6.0")
        assert process_generation_batch(g2) in {"progress", "caught_up"}
        while process_generation_batch(g2) == "progress":
            pass
        # The early candidate survived every subsequent batch untouched.
        assert (
            ReviewScoreCandidate.objects.get(generation=g2, review=target).pk
            == written.pk
        )


def test_batch_never_overwrites_candidate_provisioned_online():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()
    online_review = reviews[0]
    online_review.refresh_from_db()

    with scope(event=event):
        # Online writer already provisioned a (correct, fresh) candidate.
        ReviewScoreCandidate.objects.create(
            generation=g2,
            review=online_review,
            value=Decimal("6.0"),
            review_updated=online_review.updated,
        )
        assert process_generation_batch(g2) in {"progress", "caught_up"}

    assert ReviewScoreCandidate.objects.get(
        generation=g2, review=online_review
    ).value == Decimal("6.0")


def test_cursor_gap_is_recovered_by_sweep_before_confirm():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()
    oldest = min(reviews, key=lambda r: r.pk)

    with scope(event=event):
        # Pretend the cursor moved past a review whose candidate vanished.
        g2.cursor_position = max(r.pk for r in reviews)
        g2.save(update_fields=["cursor_position"])
        for review in reviews:
            if review.pk != oldest.pk:
                sg.refresh_review_scores(review)
        sg.refresh_review_scores(abstention)
        assert not generation_is_complete(g2)
        assert sweep_generation(g2) == 1
        assert generation_is_complete(g2)
        assert confirm_generation(g2) is True

    oldest.refresh_from_db()
    assert oldest.score == Decimal("6.0")


def test_stale_candidate_is_refreshed_by_sweep():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()
    review = reviews[0]

    with scope(event=event):
        for existing in [*reviews, abstention]:
            sg.refresh_review_scores(existing)
        candidate = ReviewScoreCandidate.objects.get(generation=g2, review=review)
        candidate.review_updated = None
        candidate.save(update_fields=["review_updated"])

        assert sweep_generation(g2) == 1
        candidate.refresh_from_db()
        assert candidate.review_updated == review.updated


def test_new_review_between_sweep_and_finish_is_not_missed():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()

    with scope(event=event):
        while process_generation_batch(g2) == "progress":
            pass
        # Reviewer adds another review while the worker is finishing up.
        late = ReviewFactory(submission=reviews[0].submission, score=None)
        late.scores.add(option)
        sg.refresh_review_scores(late)

        assert generation_is_complete(g2)  # online fan-out already provisioned it
        assert run_generation(g2.id) == "confirmed"

    late.refresh_from_db()
    assert late.score == Decimal("6.0")


def test_review_deletion_during_recalculation_still_confirms():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()
    victim = reviews[0]

    with scope(event=event):
        sg.refresh_review_scores(victim)
        victim_id = victim.pk
        victim.delete()
        assert not ReviewScoreCandidate.objects.filter(
            generation=g2, review_id=victim_id
        ).exists()

        assert run_generation(g2.id) == "confirmed"


def test_zero_review_event_confirms_immediately():
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(event=event, weight=Decimal("1.0"))

    with scope(event=event):
        sg.ensure_baseline_generation(event)
        category.weight = Decimal("3.0")
        category.save()
        g2 = sg.commit_score_generation(event)
        assert process_generation_batch(g2) == "caught_up"
        assert run_generation(g2.id) == "confirmed"


def test_run_is_idempotent_after_confirmation():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()

    with scope(event=event):
        assert run_generation(g2.id) == "confirmed"
        assert run_generation(g2.id) == "stale"

    for review in reviews:
        review.refresh_from_db()
        assert review.score == Decimal("6.0")


def test_run_reports_missing_generation():
    assert run_generation(99999999) == "missing"


def test_superseded_generation_cannot_publish_or_touch_scores():
    (event, category, option, reviews, abstention, g1, g2_stale) = _setup_event(
        review_count=2, weight_g2="2.0"
    )
    for review in [*reviews, abstention]:
        review.refresh_from_db()
    before = {review.pk: review.score for review in [*reviews, abstention]}

    # Another settings commit supersedes g2 before its worker ran.
    with scope(event=event):
        category.weight = Decimal("5.0")
        category.save()
        g3 = sg.commit_score_generation(event)

        assert run_generation(g2_stale.id) == "stale"
        g2_stale.refresh_from_db()
        assert g2_stale.status == ScoreGenerationStatus.SUPERSEDED
        for review in [*reviews, abstention]:
            review.refresh_from_db()
            assert review.score == before[review.pk]  # nothing published from g2

        assert run_generation(g3.id) == "confirmed"

    for review in reviews:
        review.refresh_from_db()
        assert review.score == Decimal("15.0")  # 3 * weight 5


def test_confirm_fails_closed_when_incomplete():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()

    with scope(event=event):
        assert confirm_generation(g2) is False
        g2.refresh_from_db()
        assert g2.status == ScoreGenerationStatus.PENDING
        for review in reviews:
            review.refresh_from_db()
            assert review.score == Decimal("3.0")  # still G1 values


def test_publish_uses_fixed_queries_regardless_of_review_count():
    event, category, option, reviews, abstention, g1, g2 = _setup_event(review_count=6)
    with scope(event=event):
        for review in [*reviews, abstention]:
            sg.refresh_review_scores(review)
        # One values query overall, then select + bulk update per chunk.
        with CaptureQueriesContext(connection) as small_chunks:
            sg._publish_candidate_scores(g2, chunk_size=3)
        with CaptureQueriesContext(connection) as one_chunk:
            sg._publish_candidate_scores(g2, chunk_size=500)

    # 7 reviews / chunk size 3 -> 1 + 3*2 = 7 queries; one chunk -> 3.
    assert len(small_chunks.captured_queries) == 7
    assert len(one_chunk.captured_queries) == 3


def test_residual_candidates_of_superseded_generation_are_cleanable():
    event, category, option, reviews, abstention, g1, g2 = _setup_event()

    with scope(event=event):
        # Give g2 some candidates, then supersede it without confirming.
        sg.refresh_review_scores(reviews[0])
        category.weight = Decimal("7.0")
        category.save()
        g3 = sg.commit_score_generation(event)
        residual = ReviewScoreCandidate.objects.filter(generation=g2)
        assert residual.exists()
        residual.delete()  # GC must not break anything.

        assert run_generation(g3.id) == "confirmed"

    for review in reviews:
        review.refresh_from_db()
        assert review.score == Decimal("21.0")
