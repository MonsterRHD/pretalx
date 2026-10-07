# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django_scopes import scope

from pretalx.submission.models import (
    ReviewScoreCandidate,
    ScoreGeneration,
    ScoreGenerationCategory,
    ScoreGenerationOption,
    ScoreGenerationStatus,
)
from tests.factories import EventFactory, ReviewFactory, ScoreGenerationFactory

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def test_score_generation_factory_sequences_per_event():
    event = EventFactory()

    first = ScoreGenerationFactory(event=event)
    second = ScoreGenerationFactory(event=event)
    other_event = ScoreGenerationFactory(event=EventFactory())

    assert first.seq == 1
    assert second.seq == 2
    assert other_event.seq == 1
    assert first.status == ScoreGenerationStatus.PENDING


def test_score_generation_confirmed_factory_sets_confirmed_at():
    generation = ScoreGenerationFactory(status=ScoreGenerationStatus.CONFIRMED)

    assert generation.confirmed_at is not None


def test_score_generation_seq_unique_per_event():
    event = EventFactory()
    ScoreGenerationFactory(event=event, seq=5)
    duplicate = ScoreGenerationFactory.build(event=event, seq=5)

    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate.save()


def test_score_generation_scoped_manager():
    event = EventFactory()
    generation = ScoreGenerationFactory(event=event)

    with scope(event=event):
        assert list(ScoreGeneration.objects.all()) == [generation]


def test_review_score_candidate_unique_per_generation_and_review():
    review = ReviewFactory()
    generation = ScoreGenerationFactory(event=review.submission.event)
    ReviewScoreCandidate.objects.create(
        generation=generation, review=review, value=Decimal("3.0")
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ReviewScoreCandidate.objects.create(
            generation=generation, review=review, value=Decimal("4.0")
        )


def test_review_score_candidate_allows_null_value():
    review = ReviewFactory()
    generation = ScoreGenerationFactory(event=review.submission.event)

    candidate = ReviewScoreCandidate.objects.create(
        generation=generation, review=review, value=None
    )

    candidate.refresh_from_db()
    assert candidate.value is None


def test_candidate_deleted_with_review():
    review = ReviewFactory()
    generation = ScoreGenerationFactory(event=review.submission.event)
    candidate = ReviewScoreCandidate.objects.create(
        generation=generation, review=review, value=Decimal("1.0")
    )

    review.delete()

    assert not ReviewScoreCandidate.objects.filter(pk=candidate.pk).exists()


def test_snapshot_and_candidates_deleted_with_generation():
    review = ReviewFactory()
    event = review.submission.event
    generation = ScoreGenerationFactory(event=event)
    frozen = ScoreGenerationCategory.objects.create(
        generation=generation,
        source_category_id=17,
        weight=Decimal("2.0"),
        active=True,
        is_independent=False,
        track_ids=[3, 7],
    )
    option = ScoreGenerationOption.objects.create(
        category=frozen, source_score_id=42, value=Decimal("5.0")
    )
    candidate = ReviewScoreCandidate.objects.create(
        generation=generation, review=review, value=Decimal("10.0")
    )

    generation.delete()

    assert not ScoreGenerationCategory.objects.filter(pk=frozen.pk).exists()
    assert not ScoreGenerationOption.objects.filter(pk=option.pk).exists()
    assert not ReviewScoreCandidate.objects.filter(pk=candidate.pk).exists()
