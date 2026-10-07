# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from django.db import migrations, models


def backfill_schedule_generations(apps, schema_editor):
    """Assign generations to existing published schedules in publication
    order and set each event's generation counter to its highest one."""
    Event = apps.get_model("event", "Event")
    Schedule = apps.get_model("schedule", "Schedule")
    for event in Event.objects.all().iterator():
        schedules = list(
            Schedule.objects.filter(event=event, published__isnull=False).order_by(
                "published", "pk"
            )
        )
        for generation, schedule in enumerate(schedules, start=1):
            schedule.generation = generation
            schedule.save(update_fields=["generation"])
        if schedules:
            event.schedule_generation = len(schedules)
            event.save(update_fields=["schedule_generation"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("event", "0046_organiser_organiser_slug_lower_unique"),
        ("schedule", "0020_schedule_generation_schedulerelease"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="schedule_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.RunPython(backfill_schedule_generations, noop),
    ]
