# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("event", "0046_organiser_organiser_slug_lower_unique"),
        ("person", "0050_user_lookup_indexes"),
        ("schedule", "0019_room_hidden"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedule",
            name="generation",
            field=models.PositiveBigIntegerField(
                blank=True, null=True, verbose_name="Generation"
            ),
        ),
        migrations.AddConstraint(
            model_name="schedule",
            constraint=models.UniqueConstraint(
                fields=("event", "generation"),
                name="schedule_unique_generation_per_event",
            ),
        ),
        migrations.CreateModel(
            name="ScheduleRelease",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(
                        auto_now_add=True, blank=True, null=True, verbose_name="Created"
                    ),
                ),
                (
                    "updated",
                    models.DateTimeField(
                        auto_now=True, blank=True, null=True, verbose_name="Updated"
                    ),
                ),
                (
                    "generation",
                    models.PositiveBigIntegerField(verbose_name="Generation"),
                ),
                ("notify_speakers", models.BooleanField(default=False)),
                (
                    "stage",
                    models.CharField(
                        choices=[
                            ("candidate", "Candidate"),
                            ("confirmed", "Confirmed"),
                            ("complete", "Complete"),
                            ("aborted", "Aborted"),
                        ],
                        default="candidate",
                        max_length=20,
                        verbose_name="Stage",
                    ),
                ),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("notifications_sent_at", models.DateTimeField(blank=True, null=True)),
                ("plugins_notified_at", models.DateTimeField(blank=True, null=True)),
                ("cache_refreshed_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("aborted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "signalled_receivers",
                    models.JSONField(blank=True, default=list),
                ),
                ("error_data", models.JSONField(blank=True, null=True)),
                ("error_timestamp", models.DateTimeField(blank=True, null=True)),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="schedule_releases",
                        to="event.event",
                    ),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="release",
                        to="schedule.schedule",
                    ),
                ),
                (
                    "wip_schedule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="source_release",
                        to="schedule.schedule",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="schedule_releases",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "Schedule releases",
                "ordering": ("-generation",),
            },
        ),
        migrations.AddConstraint(
            model_name="schedulerelease",
            constraint=models.UniqueConstraint(
                fields=("event", "generation"),
                name="schedule_release_unique_generation_per_event",
            ),
        ),
    ]
