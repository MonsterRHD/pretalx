# SPDX-FileCopyrightText: 2018-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from pretalx.common.signals import EventPluginSignal

schedule_release = EventPluginSignal()
"""
This signal allows you to trigger additional events when a new schedule
generation is confirmed (published). You will receive the confirmed schedule
and the user triggering the change (if any).

Receivers additionally receive:

- ``release``: the durable ``ScheduleRelease`` record of this publication,
- ``generation``: the monotonic per-event generation number that was
  confirmed.

Delivery is idempotent and retried: every receiver is invoked at most once
per release; if a receiver raises, it is retried (with the same confirmed
schedule and generation) until it succeeds, including after worker or
process restarts. Receivers that perform external side effects should use
the ``generation`` (or the release's schedule id) as an idempotency key to
deduplicate on their side as well. Any remaining exception is recorded on
the release record and does not roll back or block the publication itself.

As with all plugin signals, the ``sender`` keyword argument will contain the event.
Additionally, you will receive the keyword arguments ``schedule``
and ``user`` (which may be ``None``).
"""
