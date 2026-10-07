# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
"""All attendee-facing surfaces must stay on the previous generation while a
release candidate is built but unconfirmed, and switch together once the
candidate is confirmed."""

import pytest
from django_scopes import scopes_disabled

from pretalx.schedule.domain.release import (
    advance_schedule_release,
    build_schedule_release_candidate,
)
from pretalx.submission.enums import SubmissionStates
from tests.factories import SpeakerFactory, SubmissionFactory, TalkSlotFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def pending_v2(public_event_with_schedule):
    event = public_event_with_schedule
    with scopes_disabled():
        speaker = SpeakerFactory(event=event)
        submission = SubmissionFactory(event=event, state=SubmissionStates.CONFIRMED)
        submission.speakers.add(speaker)
        TalkSlotFactory(submission=submission, is_visible=True)
        release = build_schedule_release_candidate(
            event.wip_schedule, "v2", notify_speakers=False
        )
    return event, release


def test_all_surfaces_serve_v1_while_v2_is_unconfirmed(client, pending_v2):
    event, release = pending_v2

    # Public widget (unversioned and by the candidate version name)
    response = client.get(event.urls.schedule_widget_data)
    assert response.status_code == 200
    assert response.json()["version"] == "v1"
    assert len(response.json()["talks"]) == 1

    response = client.get(f"{event.urls.schedule_widget_data}?v=v2")
    assert response.status_code == 200
    assert response.json()["version"] == "v1"

    # Atom feed / changelog
    content = client.get(event.urls.feed).content.decode()
    assert content.count("<entry>") == 1
    assert "#v2" not in content

    response = client.get(event.urls.changelog)
    assert b"v2" not in response.content

    # API
    response = client.get(event.api_urls.schedules + "latest/")
    assert response.status_code == 200
    assert response.json()["version"] == "v1"

    # Public HTML schedule requesting the candidate name serves v1.
    response = client.get(
        event.urls.schedule + "v/v2/", HTTP_ACCEPT="text/html"
    )
    assert response.status_code == 200
    assert response.context["schedule"].version == "v1"


def test_all_surfaces_switch_to_v2_together_after_confirmation(
    client, pending_v2
):
    event, release = pending_v2

    with scopes_disabled():
        advance_schedule_release(release.pk)

    response = client.get(event.urls.schedule_widget_data)
    assert response.status_code == 200
    assert response.json()["version"] == "v2"
    assert len(response.json()["talks"]) == 2

    response = client.get(f"{event.urls.schedule_widget_data}?v=v2")
    assert response.json()["version"] == "v2"

    content = client.get(event.urls.feed).content.decode()
    assert content.count("<entry>") == 2
    assert "#v2" in content

    response = client.get(event.api_urls.schedules + "latest/")
    assert response.json()["version"] == "v2"

    response = client.get(
        event.urls.schedule + "v/v2/", HTTP_ACCEPT="text/html"
    )
    assert response.status_code == 200
    assert response.context["schedule"].version == "v2"
