# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
import importlib
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django_scopes import scopes_disabled

from pretalx.submission.models import (
    ScoreGeneration,
    ScoreGenerationCategory,
    ScoreGenerationOption,
    ScoreGenerationStatus,
)
from tests.factories import (
    EventFactory,
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    TrackFactory,
)

migration = importlib.import_module(
    "pretalx.submission.migrations.0114_baseline_score_generations"
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _event_with_categories():
    event = EventFactory()
    track = TrackFactory(event=event, name="Tracked")
    unrestricted = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("2.0"), active=True, is_independent=False
    )
    restricted = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("0.5"), active=False, is_independent=False
    )
    restricted.limit_tracks.add(track)
    independent = ReviewScoreCategoryFactory(
        event=event, active=True, is_independent=True
    )
    unrestricted_score = ReviewScoreFactory(category=unrestricted, value=Decimal("3.0"))
    restricted_score = ReviewScoreFactory(category=restricted, value=Decimal("1.0"))
    return (
        event,
        track,
        unrestricted,
        restricted,
        independent,
        unrestricted_score,
        restricted_score,
    )


def test_baseline_migration_creates_confirmed_snapshot():
    (
        event,
        track,
        unrestricted,
        restricted,
        independent,
        unrestricted_score,
        restricted_score,
    ) = _event_with_categories()
    review_in_event = ReviewFactory(submission__event=event, score=Decimal("6.0"))

    with scopes_disabled():
        migration.create_baseline_generations(django_apps, None)

    generation = ScoreGeneration.objects.get(event=event)
    assert generation.seq == 1
    assert generation.status == ScoreGenerationStatus.CONFIRMED
    assert generation.confirmed_at is not None

    frozen = {
        frozen.source_category_id: frozen
        for frozen in ScoreGenerationCategory.objects.filter(generation=generation)
    }
    assert set(frozen) == set(event.score_categories.values_list("id", flat=True))
    assert frozen[unrestricted.id].weight == Decimal("2.0")
    assert frozen[unrestricted.id].active is True
    assert frozen[unrestricted.id].is_independent is False
    assert frozen[unrestricted.id].track_ids == []
    assert frozen[restricted.id].weight == Decimal("0.5")
    assert frozen[restricted.id].active is False
    assert frozen[restricted.id].is_independent is False
    assert frozen[restricted.id].track_ids == [track.id]
    assert frozen[independent.id].is_independent is True
    assert frozen[independent.id].weight == Decimal("0.0")

    options = {
        option.source_score_id: option
        for option in ScoreGenerationOption.objects.filter(
            category__generation=generation
        )
    }
    assert options[unrestricted_score.id].value == Decimal("3.0")
    assert options[restricted_score.id].value == Decimal("1.0")

    review_in_event.refresh_from_db()
    assert review_in_event.score == Decimal("6.0")


def test_baseline_migration_covers_multiple_events():
    first_event = EventFactory()
    second_event = EventFactory()
    ReviewScoreCategoryFactory(event=first_event)
    ReviewScoreCategoryFactory(event=second_event)

    with scopes_disabled():
        migration.create_baseline_generations(django_apps, None)

    with scopes_disabled():
        assert ScoreGeneration.objects.filter(event=first_event).count() == 1
        assert ScoreGeneration.objects.filter(event=second_event).count() == 1


def test_baseline_migration_is_idempotent():
    event = EventFactory()
    ReviewScoreCategoryFactory(event=event)

    with scopes_disabled():
        migration.create_baseline_generations(django_apps, None)
        migration.create_baseline_generations(django_apps, None)

    with scopes_disabled():
        assert ScoreGeneration.objects.filter(event=event).count() == 1


def test_baseline_migration_handles_event_without_categories():
    event = EventFactory()
    event.score_categories.all().delete()

    with scopes_disabled():
        migration.create_baseline_generations(django_apps, None)

    generation = ScoreGeneration.objects.get(event=event)
    assert generation.status == ScoreGenerationStatus.CONFIRMED
    assert not ScoreGenerationCategory.objects.filter(generation=generation).exists()
