# File updates API

Every file (`ApplicationProfile`) has a **team** and a **timeline**. Both are
plain JSON over the existing bearer-token API, so a console, a mobile app or
another platform reads the same thing.

## Who is on a file

`GET /api/v1/application-profiles/{profile_id}/team`

```json
{
  "agent":        {"user_id": "…", "name": "Ana Lopez", "email": "…", "role": "dealer_partner", "derived_from": "intake.broker_id"},
  "underwriters": [{"user_id": "…", "name": "Jane Doe", "email": "…", "role": "loan_exec"}],
  "company":      {"id": "…", "name": "Acme Referrals", "kind": "referral_partner", "derived": true},
  "can_edit": true
}
```

A client login receives only `{"agent": {"name": …}, "underwriters": [], "company": null}`.
The agent seat is derived from the file's ownership and follows the existing
reassign actions; the desk sets underwriters (`POST`/`DELETE …/team/underwriters`)
and the company (`PUT …/team/company`). Candidates: `GET /application-profiles/team/candidates`.

## What happened on a file

`GET /api/v1/application-profiles/{profile_id}/timeline?limit=50&before=<iso>&since=<iso>`

```json
{
  "tier": "team",
  "unread_count": 2,
  "events": [
    {
      "schema": "file_event.v1",
      "id": "…", "profile_id": "…",
      "kind": "document.requested",
      "visibility": "client",
      "title": "We asked for Last 6 months bank statements",
      "body": null,
      "actor_label": "Jane Doe",
      "target_type": "requested_document", "target_id": "…",
      "meta": {},
      "created_at": "2026-09-10T15:04:05+00:00"
    }
  ]
}
```

- **Tiers.** `client` events are visible to everyone on the file including the
  client; `team` to the agent, underwriters and desk; `desk` to underwriters and
  desk. The caller's tier comes from their role; the response never includes a
  tier above it.
- **Kinds** (`file.<kind>` is the notification `event_type`): `document.requested`,
  `document.received`, `message.sent`, `status.changed`, `team.changed`,
  `note.added`, `review.completed`, `offer.sent`, `offer.answered`.
  A title says what happened; a row never carries a message body, a note or a
  review body.
- **Paging.** `before` pages back; `since` fetches what arrived after a timestamp.
- **Read state.** `POST …/timeline/seen` marks the caller's in-app rows for that
  file read. Unread comes from the same notifications the bell shows
  (`target_type = "file_event"`, `target_id = profile_id`).
- **Schema.** `schema` is bumped only when a field changes meaning, never for an
  added optional field.

`GET /api/v1/application-profiles/me/file-updates` returns the same shape across
every file the caller is on — the feed a mobile app reads instead of polling
files one by one.

`POST /api/v1/application-profiles/public/room/{token}/timeline` with
`{"passcode": "…"}` returns the `client` tier for the PIN room, no login.

## Being told

- **In-app + push.** Every event writes one `Notification` per recipient
  (`GET /api/v1/notifications`), coalesced per file for two minutes, and pushes
  to the recipient's registered devices (`POST /api/v1/devices/push-tokens`,
  `{"token": "…", "platform": "ios|android|expo"}`). The push `data` carries
  `kind`, `target_type = "file_event"`, `target_id = profile_id` and `deep_link`.
- **Live.** `GET /api/v1/communications/events` (server-sent events) emits
  `notification.created` per row and `file_event.created` with `profile_id`, so
  an open timeline refreshes without polling; `/communications/sync-state` is the
  polling fallback.
- **Email.** A one-minute drain sends one email per person per file per burst
  (agent, underwriters, the client's address, and the company's notice address
  when enabled), listing the events at that person's tier. Switches live in
  Settings under `file_updates`. Nothing here sends SMS.
