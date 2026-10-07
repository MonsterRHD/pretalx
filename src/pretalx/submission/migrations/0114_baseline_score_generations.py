# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from collections import defaultdict

from django.db import migrations, transaction
from django.utils.timezone import now
from django_scopes import scopes_disabled


def create_baseline_generations(apps, schema_editor):
    Event = apps.get_model("event", "Event")
    ReviewScoreCategory = apps.get_model("submission", "ReviewScoreCategory")
    ScoreGeneration = apps.get_model("submission", "ScoreGeneration")
    ScoreGenerationCategory = apps.get_model(
        "submission", "ScoreGenerationCategory"
    )
    ScoreGenerationOption = apps.get_model("submission", "ScoreGenerationOption")

    track_through = ReviewScoreCategory._meta.get_field(
        "limit_tracks"
    ).remote_field.through

    timestamp = now()

    # Historical models render with plain managers; scopes_disabled keeps the
    # migration usable regardless of surrounding scope configuration.
    with scopes_disabled():
        for event in Event.objects.iterator(chunk_size=500):
            # One atomic unit per event: a crash mid-freeze cannot leave a
            # confirmed baseline without its snapshot (which the idempotency
            # check below would otherwise skip).
            with transaction.atomic():
                # Idempotent: events that already have a generation are left
                # alone.
                if ScoreGeneration.objects.filter(event=event).exists():
                    continue

                generation = ScoreGeneration.objects.create(
                    event=event,
                    seq=1,
                    status="confirmed",
                    cursor_position=0,
                    confirmed_at=timestamp,
                )

                categories = list(
                    ReviewScoreCategory.objects.filter(event=event).prefetch_related(
                        "scores"
                    )
                )
                if not categories:
                    continue

                track_ids_by_category = defaultdict(list)
                for category_id, track_id in track_through.objects.filter(
                    reviewscorecategory_id__in=[
                        category.id for category in categories
                    ]
                ).values_list("reviewscorecategory_id", "track_id"):
                    track_ids_by_category[category_id].append(track_id)

                frozen_categories = [
                    ScoreGenerationCategory(
                        generation=generation,
                        source_category_id=category.id,
                        weight=category.weight,
                        active=category.active,
                        is_independent=category.is_independent,
                        track_ids=track_ids_by_category.get(category.id, []),
                    )
                    for category in categories
                ]
                ScoreGenerationCategory.objects.bulk_create(frozen_categories)

                frozen_by_source = {
                    frozen.source_category_id: frozen
                    for frozen in frozen_categories
                }
                frozen_options = [
                    ScoreGenerationOption(
                        category=frozen_by_source[category.id],
                        source_score_id=score.id,
                        value=score.value,
                    )
                    for category in categories
                    for score in category.scores.all()
                ]
                ScoreGenerationOption.objects.bulk_create(frozen_options)


class Migration(migrations.Migration):
    dependencies = [("submission", "0113_scoregenerations")]

    operations = [
        migrations.RunPython(create_baseline_generations, migrations.RunPython.noop)
    ]
