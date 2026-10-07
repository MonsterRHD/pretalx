# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_scopes import scope

from pretalx.submission.domain.score_generation import (
    commit_score_generation,
    confirmed_generation,
    ensure_baseline_generation,
    freeze_score_settings,
    load_frozen_rules,
    pending_generations,
)
from pretalx.submission.models import (
    ScoreGeneration,
    ScoreGenerationCategory,
    ScoreGenerationOption,
    ScoreGenerationStatus,
)
from tests.factories import (
    EventFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    TrackFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def test_commit_creates_baseline_and_pending_generation():
    event = EventFactory()
    live_category = event.score_categories.first()

    with scope(event=event):
        generation = commit_score_generation(event)
        baseline = confirmed_generation(event)

    assert generation.status == ScoreGenerationStatus.PENDING
    assert generation.seq == 2
    assert baseline.seq == 1
    assert baseline.status == ScoreGenerationStatus.CONFIRMED
    assert baseline.confirmed_at is not None
    frozen = ScoreGenerationCategory.objects.get(
        generation=generation, source_category_id=live_category.id
    )
    assert frozen.weight == Decimal("1.0")
    frozen_option_ids = set(
        ScoreGenerationOption.objects.filter(
            category__generation=generation
        ).values_list("source_score_id", flat=True)
    )
    assert frozen_option_ids == set(live_category.scores.values_list("id", flat=True))


def test_commit_freezes_weight_active_independent_and_tracks():
    event = EventFactory()
    track = TrackFactory(event=event)
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("2.5"), active=False, is_independent=False
    )
    category.limit_tracks.add(track)
    score = ReviewScoreFactory(category=category, value=Decimal("4.0"))

    with scope(event=event):
        frozen = freeze_score_settings(event)

    entry = next(item for item in frozen if item["source_category_id"] == category.id)
    assert entry["weight"] == Decimal("2.5")
    assert entry["active"] is False
    assert entry["is_independent"] is False
    assert entry["track_ids"] == [track.id]
    assert {"source_score_id": score.id, "value": Decimal("4.0")} in entry["options"]


def test_consecutive_commits_supersede_previous_pending():
    event = EventFactory()

    with scope(event=event):
        first = commit_score_generation(event)
        second = commit_score_generation(event)

    first.refresh_from_db()
    assert first.status == ScoreGenerationStatus.SUPERSEDED
    assert second.status == ScoreGenerationStatus.PENDING
    assert second.seq == first.seq + 1
    assert [generation.seq for generation in pending_generations(event)] == [second.seq]
    assert confirmed_generation(event).seq == 1


def test_snapshot_survives_live_category_and_option_changes():
    event = EventFactory()
    category = ReviewScoreCategoryFactory(event=event, weight=Decimal("1.0"))
    score = ReviewScoreFactory(category=category, value=Decimal("3.0"))

    with scope(event=event):
        generation = commit_score_generation(event)
        category.weight = Decimal("5.0")
        category.save()
        score.value = Decimal("9.0")
        score.save()

    frozen_category = ScoreGenerationCategory.objects.get(
        generation=generation, source_category_id=category.id
    )
    frozen_option = ScoreGenerationOption.objects.get(
        category__generation=generation, source_score_id=score.id
    )
    assert frozen_category.weight == Decimal("1.0")
    assert frozen_option.value == Decimal("3.0")

    rules = load_frozen_rules(generation)
    assert rules.categories[category.id].weight == Decimal("1.0")
    assert rules.options[score.id][1] == Decimal("3.0")


def test_snapshot_survives_live_category_and_track_deletion():
    event = EventFactory()
    event.score_categories.all().delete()
    track = TrackFactory(event=event)
    category = ReviewScoreCategoryFactory(event=event, weight=Decimal("1.0"))
    category.limit_tracks.add(track)
    score = ReviewScoreFactory(category=category, value=Decimal("2.0"))

    with scope(event=event):
        generation = commit_score_generation(event)
        category_id, score_id, track_id = category.id, score.id, track.id
        score.delete()
        # Queryset delete bypasses the "one non-independent category" guard,
        # which is irrelevant to snapshot persistence.
        event.score_categories.filter(pk=category.id).delete()
        track.delete()

    frozen_category = ScoreGenerationCategory.objects.get(
        generation=generation, source_category_id=category_id
    )
    assert frozen_category.track_ids == [track_id]
    assert ScoreGenerationOption.objects.filter(source_score_id=score_id).exists()
    # Loading rules does not touch the deleted live rows.
    rules = load_frozen_rules(generation)
    assert rules.categories[category_id].track_ids == frozenset({track_id})


def test_ensure_baseline_is_idempotent():
    event = EventFactory()

    with scope(event=event):
        created = ensure_baseline_generation(event)
        second_call = ensure_baseline_generation(event)

    assert created is not None
    assert created.status == ScoreGenerationStatus.CONFIRMED
    assert second_call is None
    assert ScoreGeneration.objects.filter(event=event).count() == 1


def test_freeze_uses_fixed_query_count():
    event = EventFactory()
    extra = ReviewScoreCategoryFactory(event=event)
    ReviewScoreFactory(category=extra, value=Decimal("1.0"))
    ReviewScoreFactory(category=extra, value=Decimal("2.0"))
    track = TrackFactory(event=event)
    extra.limit_tracks.add(track)

    # 3 queries regardless of category/option count: categories, prefetched
    # options, prefetched tracks.
    with scope(event=event), CaptureQueriesContext(connection) as context:
        freeze_score_settings(event)

    assert len(context.captured_queries) == 3
