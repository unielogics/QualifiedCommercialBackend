from __future__ import annotations

from unittest.mock import MagicMock

from app.services.email.user_inbox_sync import _list_message_refs


def test_shared_mailbox_sync_drains_every_gmail_page() -> None:
    first = MagicMock()
    first.execute.return_value = {
        "messages": [{"id": "newest"}],
        "nextPageToken": "next-page",
    }
    second = MagicMock()
    second.execute.return_value = {"messages": [{"id": "older"}]}
    listing = MagicMock(side_effect=[first, second])
    messages = MagicMock()
    messages.list = listing
    users = MagicMock()
    users.messages.return_value = messages
    service = MagicMock()
    service.users.return_value = users

    assert _list_message_refs(service) == [{"id": "newest"}, {"id": "older"}]
    assert listing.call_args_list[0].kwargs.get("pageToken") is None
    assert listing.call_args_list[1].kwargs["pageToken"] == "next-page"
