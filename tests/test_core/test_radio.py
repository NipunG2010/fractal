"""Test the ``fractal.core.radio`` module.

Cross-node behavior runs against two real node identities sharing the one
central database (the ``radio_pair`` fixture) -- no resolver mocks.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fractal.core.radio import Radio

__all__ = [
    'test_init_seeds_default_channels',
    'test_init_subscribes_within_subtree_only',
    'test_send_stamps_sender_session',
    'test_send_to_self',
    'test_send_self_targeted_node_behaves_as_bare',
    'test_send_routes_to_target',
    'test_send_parent_flag',
    'test_send_unknown_node',
    'test_send_nonexistent_channel',
    'test_send_rejects_invalid_priority',
    'test_sent_lists_outbound_mail',
    'test_sent_includes_own_replies',
    'test_messages_query',
    'test_messages_channel_filter',
    'test_messages_read_filter',
    'test_acting_marks_message_read',
    'test_read_returns_counts',
    'test_read_multiple_uuids_dedupes_without_partial_receipts',
    'test_read_channel_selector_reads_and_receipts',
    'test_read_feed_selector_catches_up_subscriptions',
    'test_read_receipts_attribute_to_reader_not_mailbox',
    'test_read_channel_selector_respects_read_only',
    'test_feed_fans_out_with_sources',
    'test_reply_inherits_subject_and_priority',
    'test_reply_lands_in_parents_channel_space',
    'test_reply_to_inbox_message_reaches_the_sender',
    'test_thread_structure',
    'test_uuid_resolves_globally',
    'test_cross_node_read_respects_read_only',
    'test_write_only_permission',
    'test_react_and_re_react',
    'test_react_invalid_value',
    'test_custom_channel',
    'test_custom_channel_rejects_reserved',
    'test_custom_channel_rejects_duplicate',
    'test_channel_delete_scoped_to_owner',
    'test_channel_delete_refuses_messages_without_force',
    'test_subscribe_and_unsubscribe',
    'test_subscribe_validates_target',
    'test_archive_round_trip_per_saver',
    'test_archive_survives_unsend',
    'test_unsave_not_found_stays_terse',
    'test_not_found_by_uuid',
    'test_unsend_deletes_message',
    'test_unsend_deletes_replies_and_reacts',
    'test_unsend_refuses_thread_without_force',
    'test_unsend_wrong_sender',
    'test_unsend_force_aborts_on_concurrent_reply',
]


def test_init_seeds_default_channels(radio: Radio) -> None:
    """Init seeds public, private, inbox, outbox channels."""
    channels = radio.channels()
    names = {channel['channel'] for channel in channels}
    assert names == {'public', 'private', 'inbox', 'outbox'}
    # verify permission bits
    by_name = {channel['channel']: channel for channel in channels}
    assert by_name['public']['read_only'] == 0
    assert by_name['public']['write_only'] == 0
    assert by_name['private']['read_only'] == 1
    assert by_name['private']['write_only'] == 1
    assert by_name['inbox']['read_only'] == 1
    assert by_name['inbox']['write_only'] == 0
    assert by_name['outbox']['read_only'] == 0
    assert by_name['outbox']['write_only'] == 1


def test_init_subscribes_within_subtree_only(radio_pair: tuple[Radio, Radio]) -> None:
    """Init auto-subscribes to the parent and direct children -- never siblings.

    The central registry holds the whole tree, so the child-subscribe pass
    must match on the subtree prefix: a same-depth node in a sibling subtree
    (which depth-counting alone would catch) is not a child.
    """
    root, peer = radio_pair
    peer_branch = peer.node._branch
    root_branch = root.node._branch
    # the fixture mirrors production: peer subscribed to its parent at init,
    # root subscribed to the peer (child_add)
    assert {s['target'] for s in peer.subs()} == {root_branch}
    assert {s['target'] for s in root.subs()} == {peer_branch}
    # a same-depth node in a *sibling* subtree, registered and seeded
    foreign = f'{peer_branch}x.kid'
    root.db.merge({'node': foreign, 'status': 'idle'}, 'nodes')
    root.db.merge(
        {'node': foreign, 'channel': 'public', 'read_only': 0, 'write_only': 0},
        'channels',
    )
    # re-init is idempotent and must not subscribe across subtrees
    peer.init()
    assert {s['target'] for s in peer.subs()} == {root_branch}


def test_send_stamps_sender_session(radio: Radio) -> None:
    """Sends and replies record the acting node's live agent session.

    The stamp ties a message to the conversation that wrote it; a node with
    no configured agent or no woven session stamps NULL.
    """
    bare = radio.send(channel='public', subject='s', data='d', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': bare})
    assert row['session'] is None
    # a live session (and an agent command with flags) stamps its base session
    radio.node.config_set(agent='claude --verbose')
    radio.node.session_set('claude', 'sess-1')
    stamped = radio.send(channel='public', subject='s2', data='d2', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': stamped})
    assert row['session'] == 'sess-1'
    reply_uuid, _, _ = radio.reply(stamped, 'on it')
    [row] = radio.node.db.read('messages', where={'message_uuid': reply_uuid})
    assert row['session'] == 'sess-1'


def test_send_to_self(radio: Radio) -> None:
    """A bare send lands in the own inbox; a channel send in that channel."""
    msg_uuid = radio.send(
        subject='test subject',
        data='test data',
        priority=5,
    )
    assert isinstance(msg_uuid, str)
    assert len(msg_uuid) == 8
    rows = radio.messages(channel='inbox')
    assert len(rows) == 1
    assert rows[0]['node'] == radio.node._branch
    assert rows[0]['subject'] == 'test subject'
    assert rows[0]['data'] == 'test data'
    assert rows[0]['priority'] == 5
    assert rows[0]['channel'] == 'inbox'
    # the own write-only outbox is reachable for the owner
    radio.send(channel='outbox', subject='broadcast', data='d', priority=8)
    outbox = radio.messages(channel='outbox')
    assert len(outbox) == 1
    assert outbox[0]['priority'] == 8


def test_send_self_targeted_node_behaves_as_bare(radio: Radio) -> None:
    """A self-targeted ``node`` normalizes to the bare (own-inbox) send."""
    uuid = radio.send(
        radio.node._branch,
        subject='s',
        data='d',
        priority=0,
    )
    [row] = radio.db.read('messages', where={'message_uuid': uuid})
    assert row['node'] == radio.node._branch


def test_send_routes_to_target(radio_pair: tuple[Radio, Radio]) -> None:
    """A targeted send lands in the recipient's channel-space, not the sender's."""
    root, peer = radio_pair
    peer_branch = peer.node._branch
    uuid = root.send(peer_branch, subject='task', data='do it', priority=7)
    # the row's node column names the host; the sender keeps no mailbox copy
    [row] = root.db.read('messages', where={'message_uuid': uuid})
    assert row['node'] == peer_branch
    assert row['sender'] == root.node._branch
    assert [m['message_uuid'] for m in peer.messages(channel='inbox')] == [uuid]
    assert root.messages(channel='inbox') == []


def test_send_parent_flag(radio_pair: tuple[Radio, Radio]) -> None:
    """``parent=True`` resolves the dotted parent and lands in its inbox."""
    root, peer = radio_pair
    uuid = peer.send(parent=True, subject='done', data='summary', priority=5)
    [row] = peer.db.read('messages', where={'message_uuid': uuid})
    assert row['node'] == root.node._branch
    assert row['channel'] == 'inbox'


def test_send_unknown_node(radio: Radio) -> None:
    """Sending to an unregistered node is rejected."""
    with pytest.raises(ValueError, match='Node not found'):
        radio.send('main.ghost', subject='s', data='d', priority=0)


def test_send_nonexistent_channel(radio: Radio) -> None:
    """Sending to a nonexistent channel raises ValueError."""
    with pytest.raises(ValueError, match='not found'):
        radio.send(
            channel='nonexistent',
            subject='s',
            data='d',
            priority=0,
        )


def test_send_rejects_invalid_priority(radio: Radio) -> None:
    """Send rejects priority outside 0-10."""
    with pytest.raises(ValueError, match='must be 0-10'):
        radio.send(subject='s', data='d', priority=11)
    with pytest.raises(ValueError, match='must be 0-10'):
        radio.send(subject='s', data='d', priority=-1)
    # boundary values are accepted
    radio.send(subject='s', data='d', priority=0)
    radio.send(subject='s', data='d', priority=10)


def test_sent_lists_outbound_mail(radio_pair: tuple[Radio, Radio]) -> None:
    """``sent`` lists own-authored messages with the recipient in ``node``."""
    root, peer = radio_pair
    peer_branch = peer.node._branch
    to_peer = root.send(peer_branch, subject='for you', data='d', priority=6)
    to_self = root.send(channel='private', subject='note', data='d', priority=2)
    peer.send(parent=True, subject='not yours', data='d', priority=5)
    # both outbound messages list, recipient-attributed, priority first
    rows = root.sent()
    assert [r['message_uuid'] for r in rows] == [to_peer, to_self]
    assert [r['node'] for r in rows] == [peer_branch, root.node._branch]
    # the channel filter narrows to one host's channel
    inbox_only = root.sent(channel='inbox')
    assert [r['message_uuid'] for r in inbox_only] == [to_peer]
    # recent flips to newest-first
    recent = root.sent(recent=True)
    assert [r['message_uuid'] for r in recent] == [to_self, to_peer]


def test_sent_includes_own_replies(radio_pair: tuple[Radio, Radio]) -> None:
    """``sent`` lists authored replies too, wherever they thread.

    The mailbox listings hide a reply that threads in place behind its
    parent's reply count, but ``sent`` is the review surface for what this
    node wrote -- replies must list beside the top-level posts.
    """
    root, peer = radio_pair
    # an in-place reply into the peer's readable channel
    post = peer.send(channel='public', subject='post', data='d', priority=3)
    in_place, _, _ = root.reply(post, 'seen')
    # a follow-up threaded onto the own outbox post
    cast = root.send(channel='outbox', subject='cast', data='d', priority=3)
    follow_up, _, _ = root.reply(cast, 'follow-up')
    uuids = {r['message_uuid'] for r in root.sent()}
    assert {in_place, cast, follow_up} <= uuids


def test_messages_query(radio: Radio) -> None:
    """Messages are ordered by priority DESC, created_at ASC."""
    radio.send(subject='low', data='d', priority=1)
    radio.send(subject='high', data='d', priority=9)
    radio.send(subject='mid', data='d', priority=5)
    rows = radio.messages()
    priorities = [r['priority'] for r in rows]
    assert priorities == [9, 5, 1]


def test_messages_channel_filter(radio: Radio) -> None:
    """Channel filter returns only matching messages."""
    radio.send(subject='inbox msg', data='d', priority=0)
    radio.send(channel='outbox', subject='outbox msg', data='d', priority=0)
    inbox = radio.messages(channel='inbox')
    assert len(inbox) == 1
    assert inbox[0]['subject'] == 'inbox msg'
    outbox = radio.messages(channel='outbox')
    assert len(outbox) == 1
    assert outbox[0]['subject'] == 'outbox msg'


def test_messages_read_filter(radio: Radio) -> None:
    """Read filter: None=all, False=unread, True=read (receipt-backed)."""
    radio.send(subject='s', data='d', priority=0)
    # all messages (unread)
    assert len(radio.messages(read=None)) == 1
    assert len(radio.messages(read=False)) == 1
    assert len(radio.messages(read=True)) == 0
    # mark as read -- the owner's receipt lands in reads like everyone else's
    uuid = radio.messages()[0]['message_uuid']
    radio.read(uuid)
    assert len(radio.messages(read=None)) == 1
    assert len(radio.messages(read=False)) == 0
    assert len(radio.messages(read=True)) == 1


@pytest.mark.parametrize('act', ['read', 'react', 'reply'])
def test_acting_marks_message_read(radio: Radio, act: str) -> None:
    """Reading, reacting, and replying all clear a message from the unread set.

    Every acknowledgment lands the actor's receipt in ``reads`` -- otherwise
    SYNC resurfaces the same items every pass, burning tokens.
    """
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    assert len(radio.messages(read=False)) == 1
    if act == 'read':
        radio.read(uuid)
    elif act == 'react':
        radio.react(uuid, 1)
    else:
        radio.reply(uuid, 'ack')
    assert len(radio.messages(read=False)) == 0
    assert len(radio.messages(read=True)) == 1


def test_read_returns_counts(radio: Radio) -> None:
    """Read returns the full message with reply/react counts and a receipt."""
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    radio.reply(uuid, 'r1')
    radio.react(uuid, 1)
    [message] = radio.read(uuid)
    assert message['subject'] == 's'
    assert message['data'] == 'd'
    assert message['replies'] == 1
    assert message['pos_reacts'] == 1
    assert message['neg_reacts'] == 0
    # the reader's receipt is on record
    receipt = {'message_id': message['message_id'], 'node': radio.node._branch}
    assert radio.db.exists('reads', where=receipt)


def test_read_multiple_uuids_dedupes_without_partial_receipts(
    radio: Radio,
) -> None:
    """Read returns named UUIDs once, in order; a failed lookup receipts nothing.

    Duplicate UUIDs collapse to their first occurrence, and receipts land
    only after every lookup resolves -- a not-found UUID anywhere in the
    call leaves even its resolvable companions unread.
    """
    first = radio.send(subject='one', data='d', priority=0)
    second = radio.send(subject='two', data='d', priority=0)
    messages = radio.read(first, second, first)
    assert [m['message_uuid'] for m in messages] == [first, second]
    assert len(radio.messages(read=True)) == 2
    # a not-found UUID aborts the whole read before any receipt
    third = radio.send(subject='three', data='d', priority=0)
    with pytest.raises(ValueError, match='not found'):
        radio.read(third, 'DEADBEEF')
    assert len(radio.messages(read=False)) == 1


def test_read_channel_selector_reads_and_receipts(radio: Radio) -> None:
    """``read(channel=...)`` returns the channel's rows and receipts exactly them.

    The ``unread`` narrowing follows this reader's receipts, so a second
    unread read returns nothing new; rows outside the selection stay unread.
    """
    kept = radio.send(subject='kept', data='d', priority=2)
    swept = radio.send(subject='swept', data='d', priority=5)
    radio.send(channel='private', subject='note', data='d', priority=5)
    # the selector reads the whole channel, priority first
    messages = radio.read(channel='inbox', unread=True)
    assert [m['message_uuid'] for m in messages] == [swept, kept]
    # receipts landed for exactly the returned rows: nothing unread remains
    assert radio.read(channel='inbox', unread=True) == []
    assert len(radio.messages(channel='private', read=False)) == 1
    # an explicit UUID returns regardless of its read state
    [again] = radio.read(swept)
    assert again['subject'] == 'swept'
    # without ``unread`` the selector re-reads the full channel
    assert len(radio.read(channel='private')) == 1
    assert radio.messages(channel='private', read=False) == []


def test_read_feed_selector_catches_up_subscriptions(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """``read(feed=True, unread=True)`` drains the unread feed, once.

    The fan-out mirrors ``feed`` (readable subscribed channels, priority
    order); receipts land for the returned rows, for this reader only.
    """
    root, peer = radio_pair
    cast = peer.send(channel='outbox', subject='cast', data='d', priority=5)
    post = peer.send(channel='public', subject='post', data='d', priority=3)
    messages = root.read(feed=True, unread=True)
    assert [m['message_uuid'] for m in messages] == [cast, post]
    # the catch-up is complete: nothing unread remains for this reader
    assert root.read(feed=True, unread=True) == []
    # the peer's own unread state never moved
    assert len(peer.messages(channel='outbox', read=False)) == 1


def test_read_receipts_attribute_to_reader_not_mailbox(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """Selector reads of another mailbox receipt as the reader, never the owner.

    ``node`` picks whose channel-space is viewed; the receipt names the
    reading node, so an operator peek can never consume the owner's
    unread state.
    """
    root, peer = radio_pair
    peer_branch = peer.node._branch
    cast = peer.send(channel='outbox', subject='cast', data='d', priority=5)
    [message] = root.read(node=peer_branch, channel='outbox')
    assert message['message_uuid'] == cast
    # the receipt names the root, and only the root
    [receipt] = root.db.read('reads', where={'message_id': message['message_id']})
    assert receipt['node'] == root.node._branch
    # the owner's unread state never moved
    assert len(peer.messages(channel='outbox', read=False)) == 1
    # the feed selector honors ``node`` too: the peer views the ROOT's feed
    # (the root's subscriptions carry the peer's cast, not the root's own post)
    note = root.send(channel='outbox', subject='note', data='d', priority=5)
    viewed = peer.read(node=root.node._branch, feed=True)
    read_uuids = {m['message_uuid'] for m in viewed}
    assert cast in read_uuids
    assert note not in read_uuids


def test_read_channel_selector_respects_read_only(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A read-only channel selector is owner-only, like the by-UUID rule."""
    root, peer = radio_pair
    root.send(peer.node._branch, subject='secret', data='d', priority=0)
    with pytest.raises(PermissionError, match='read-only'):
        root.read(node=peer.node._branch, channel='inbox')
    # the owner reads its own read-only channel freely
    [message] = peer.read(channel='inbox')
    assert message['subject'] == 'secret'


def test_feed_fans_out_with_sources(radio_pair: tuple[Radio, Radio]) -> None:
    """Feed merges subscribed channels, attributing each row to its source.

    Listing is always passive: the unread view is stable across
    calls -- and under ``limit`` -- until the reader consumes rows through
    ``read``, and even then only for that reader.
    """
    root, peer = radio_pair
    peer_branch = peer.node._branch
    cast = peer.send(channel='outbox', subject='cast', data='d', priority=5)
    post = peer.send(channel='public', subject='post', data='d', priority=3)
    rows = root.feed(read=False)
    assert {r['message_uuid'] for r in rows} == {cast, post}
    assert {r['node'] for r in rows} == {peer_branch}
    # listing is passive: the unread view is unchanged, capped or not
    assert len(root.feed(limit=1, read=False)) == 1
    assert {r['message_uuid'] for r in root.feed(read=False)} == {cast, post}
    # reading is the consuming act, and only for this reader
    root.read(cast, post)
    assert root.feed(read=False) == []
    assert len(peer.messages(channel='outbox', read=False)) == 1


def test_reply_inherits_subject_and_priority(radio: Radio) -> None:
    """Reply inherits 'Re: subject' and priority from parent."""
    radio.send(subject='original', data='d', priority=4)
    uuid = radio.messages()[0]['message_uuid']
    reply_uuid, node, channel = radio.reply(uuid, 'reply data')
    # the return carries the resolved destination beside the UUID
    assert isinstance(reply_uuid, str)
    assert len(reply_uuid) == 8
    assert (node, channel) == (radio.node._branch, 'inbox')
    [reply] = radio.db.read('messages', where={'message_uuid': reply_uuid})
    assert reply['subject'] == 'Re: original'
    assert reply['priority'] == 4
    assert reply['data'] == 'reply data'
    assert reply['parent_message_uuid'] == uuid


def test_reply_lands_in_parents_channel_space(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A reply to another node's readable channel stays in that channel-space.

    Replying into another node's write-only channel never pierces it -- the
    reply reroutes to the owner's inbox as a conversation turn.
    """
    root, peer = radio_pair
    peer_branch = peer.node._branch
    post = peer.send(channel='public', subject='post', data='d', priority=3)
    reply_uuid, node, channel = root.reply(post, 'seen')
    [reply] = root.db.read('messages', where={'message_uuid': reply_uuid})
    assert reply['node'] == peer_branch
    assert reply['sender'] == root.node._branch
    assert (node, channel) == (peer_branch, 'public')
    # a non-owner's reply into a write-only channel reaches the owner's
    # inbox instead of the channel itself
    cast = peer.send(channel='outbox', subject='cast', data='d', priority=3)
    cast_uuid, node, channel = root.reply(cast, 'ack')
    [rerouted] = root.db.read('messages', where={'message_uuid': cast_uuid})
    assert (rerouted['node'], rerouted['channel']) == (peer_branch, 'inbox')
    assert (node, channel) == (peer_branch, 'inbox')


def test_reply_to_inbox_message_reaches_the_sender(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A reply to a message in my inbox lands in the original sender's inbox.

    Conversation semantics: answering a message someone sent me must reach
    the counterparty, threaded, not self-thread into my own inbox where the
    sender never sees it.
    """
    root, peer = radio_pair
    root_branch = root.node._branch
    peer_branch = peer.node._branch
    # peer asks in root's inbox; root's answer must land with the peer
    ask = peer.send(node=root_branch, subject='ask', data='d', priority=3)
    answer, _, _ = root.reply(ask, 'answer')
    [reply] = root.db.read('messages', where={'message_uuid': answer})
    assert reply['node'] == peer_branch
    assert reply['channel'] == 'inbox'
    assert reply['sender'] == root_branch
    assert reply['subject'] == 'Re: ask'
    assert reply['parent_message_uuid'] == ask
    # a reply to my own outbox message stays self-threaded
    cast = root.send(channel='outbox', subject='cast', data='d', priority=3)
    note, _, _ = root.reply(cast, 'follow-up')
    [reply] = root.db.read('messages', where={'message_uuid': note})
    assert reply['node'] == root_branch
    assert reply['channel'] == 'outbox'


def test_thread_structure(radio: Radio) -> None:
    """Thread returns flat list with depth for indentation."""
    radio.send(subject='root', data='d', priority=0)
    root_uuid = radio.messages()[0]['message_uuid']
    # reply to root, then reply to the reply
    reply_uuid, _, _ = radio.reply(root_uuid, 'reply 1')
    radio.reply(reply_uuid, 'reply 2')
    # the thread resolves from any message in it (walks up to the root)
    thread = radio.thread(reply_uuid)
    assert len(thread) == 3
    assert thread[0]['depth'] == 0
    assert thread[0]['subject'] == 'root'
    assert thread[1]['depth'] == 1
    assert thread[2]['depth'] == 2


def test_uuid_resolves_globally(radio_pair: tuple[Radio, Radio]) -> None:
    """A UUID resolves from any node without naming its host.

    UUIDs are unique across the whole tree (one ``messages`` table), so a
    feed-discovered message is readable directly.
    """
    root, peer = radio_pair
    cast = peer.send(channel='outbox', subject='cast', data='body', priority=5)
    [message] = root.read(cast)
    assert message['node'] == peer.node._branch
    assert message['data'] == 'body'


@pytest.mark.parametrize('method', ['read', 'thread', 'react', 'save'])
def test_cross_node_read_respects_read_only(
    radio_pair: tuple[Radio, Radio],
    method: str,
) -> None:
    """Cross-node by-UUID access enforces the read-only "owner only" rule.

    ``feed`` and ``subscribe`` already bar a non-owner from a read-only
    channel (``inbox``/``private``), so reaching a message there by UUID must
    be denied too -- otherwise the restriction leaks (a UUID holder could ack
    or archive a message they are not allowed to read). Readable channels
    (``outbox``) stay reachable. ``thread`` exempts conversation
    participants, so its denial needs a true bystander.
    """
    root, peer = radio_pair
    # a read-only message in the peer's inbox and a readable outbox cast
    private_uuid = root.send(peer.node._branch, subject='secret', data='d', priority=0)
    public_uuid = peer.send(channel='outbox', subject='cast', data='d', priority=0)
    extra = (1,) if method == 'react' else ()
    call = getattr(root, method)
    if method == 'thread':
        # root sent the inbox message, so it may thread that conversation;
        # a peer-only private note keeps root a non-participant
        secret = peer.send(channel='private', subject='note', data='d', priority=0)
        with pytest.raises(PermissionError, match='read-only'):
            call(secret)
    else:
        with pytest.raises(PermissionError, match='read-only'):
            call(private_uuid, *extra)
    # the readable channel is still reachable cross-node
    call(public_uuid, *extra)
    # the peer reads its own inbox freely (owner only, not owner never)
    [note] = peer.read(private_uuid)
    assert note['subject'] == 'secret'


def test_write_only_permission(radio_pair: tuple[Radio, Radio]) -> None:
    """Owner can send to own write_only channel; others are rejected."""
    root, peer = radio_pair
    peer.send(channel='outbox', subject='self publish', data='d', priority=0)
    assert len(peer.messages(channel='outbox')) == 1
    with pytest.raises(PermissionError, match='write-only'):
        root.send(
            peer.node._branch,
            channel='outbox',
            subject='rejected',
            data='d',
            priority=0,
        )


def test_react_and_re_react(radio: Radio) -> None:
    """React writes to reacts table; re-reacting changes value."""
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    radio.react(uuid, 1)
    reacts = radio.db.read('reacts')
    assert len(reacts) == 1
    assert reacts[0]['value'] == 1
    # re-react changes value
    radio.react(uuid, -1)
    reacts = radio.db.read('reacts')
    assert len(reacts) == 1
    assert reacts[0]['value'] == -1


def test_react_invalid_value(radio: Radio) -> None:
    """React rejects values other than 1 or -1."""
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    with pytest.raises(ValueError, match='must be 1 or -1'):
        radio.react(uuid, 0)


def test_custom_channel(radio: Radio) -> None:
    """Custom channels can be registered."""
    radio.channel('alerts', read_only=True, write_only=True)
    channels = radio.channels()
    alerts = next(channel for channel in channels if channel['channel'] == 'alerts')
    assert alerts['read_only'] == 1
    assert alerts['write_only'] == 1


def test_custom_channel_rejects_reserved(radio: Radio) -> None:
    """Custom channels cannot use reserved names."""
    with pytest.raises(ValueError, match='reserved'):
        radio.channel('public')


def test_custom_channel_rejects_duplicate(radio: Radio) -> None:
    """Re-creating a channel raises instead of silently overwriting its flags.

    ``create`` is create, not a lossy upsert: re-running it must not reset a
    channel's read/write flags (a no-flag re-create of a ``--read-only`` channel
    would otherwise downgrade it to public) nor mint a new ``channel_id``.
    """
    radio.channel('alerts', read_only=True)
    before = next(c for c in radio.channels() if c['channel'] == 'alerts')
    # a duplicate create is refused with a clear error
    with pytest.raises(ValueError, match='already exists'):
        radio.channel('alerts')
    # the original flags and id are untouched (no silent downgrade)
    after = next(c for c in radio.channels() if c['channel'] == 'alerts')
    assert after['read_only'] == 1
    assert after['channel_id'] == before['channel_id']


def test_channel_delete_scoped_to_owner(radio_pair: tuple[Radio, Radio]) -> None:
    """Deleting a custom channel cascades its data -- in one channel-space only.

    The schema has no ON DELETE CASCADE, so channel_delete removes the
    channel's messages, reacts, and receipts itself; with every node's
    channels in one table, the cascade must not touch another node's
    same-named channel.
    """
    root, peer = radio_pair
    root.channel('team')
    peer.channel('team')
    root_msg = root.send(channel='team', subject='ours', data='d', priority=5)
    peer_msg = peer.send(channel='team', subject='theirs', data='d', priority=5)
    root.react(root_msg, 1)
    root.channel_delete('team', force=True)
    # the root's channel-space is gone: message, react, receipt, channel row
    assert not root.db.exists('messages', where={'message_uuid': root_msg})
    assert root.db.read('reacts') == []
    assert not root.db.exists(
        'channels', where={'node': root.node._branch, 'channel': 'team'}
    )
    # the peer's same-named channel and message survive untouched
    assert peer.db.exists('messages', where={'message_uuid': peer_msg})
    assert [c['channel'] for c in peer.channels() if c['channel'] == 'team']


def test_channel_delete_refuses_messages_without_force(radio: Radio) -> None:
    """Channel delete refuses a channel that holds messages unless forced.

    Mirrors ``unsend`` -- the cascade also removes other nodes' replies,
    so it is refused without ``--force``, leaving the channel intact.
    """
    radio.channel('team')
    radio.send(channel='team', subject='s', data='d', priority=5)
    # a channel with messages is refused, leaving it and them intact
    with pytest.raises(ValueError, match='use --force'):
        radio.channel_delete('team')
    assert len(radio.messages(channel='team')) == 1
    assert [c['channel'] for c in radio.channels() if c['channel'] == 'team']
    # --force deletes the channel and cascades its messages
    radio.channel_delete('team', force=True)
    assert [c['channel'] for c in radio.channels() if c['channel'] == 'team'] == []
    assert radio.messages(channel='team') == []


def test_subscribe_and_unsubscribe(radio: Radio) -> None:
    """Subscribe writes sub rows for readable channels; unsubscribe removes them."""
    branch = radio.node._branch
    radio.subscribe(branch, channel='outbox')
    radio.subscribe(branch, channel='public')
    channels = {s['channel'] for s in radio.subs() if s['target'] == branch}
    assert channels == {'outbox', 'public'}
    # unsubscribe one channel, then all
    radio.unsubscribe(branch, channel='outbox')
    remaining = [s['channel'] for s in radio.subs() if s['target'] == branch]
    assert remaining == ['public']
    radio.unsubscribe(branch)
    assert [s for s in radio.subs() if s['target'] == branch] == []


def test_subscribe_validates_target(radio: Radio) -> None:
    """Subscribe rejects unknown nodes, channels, and unreadable channels."""
    branch = radio.node._branch
    with pytest.raises(ValueError, match='Node not found'):
        radio.subscribe('main.ghost', channel='public')
    with pytest.raises(ValueError, match='Channel not found'):
        radio.subscribe(branch, channel='nope')
    with pytest.raises(ValueError, match='cannot be subscribed'):
        radio.subscribe(branch, channel='private')


def test_archive_round_trip_per_saver(radio_pair: tuple[Radio, Radio]) -> None:
    """Save copies a message per saver; unsave removes only the own copy."""
    root, peer = radio_pair
    cast = peer.send(channel='outbox', subject='keep', data='body', priority=5)
    root.save(cast)
    peer.save(cast)
    # each saver holds its own copy, attributed to the source host
    [root_copy] = root.saved()
    assert root_copy['message_uuid'] == cast
    assert root_copy['owner'] == peer.node._branch
    assert len(peer.saved()) == 1
    # re-saving is idempotent; unsave removes only the caller's copy
    root.save(cast)
    assert len(root.saved()) == 1
    root.unsave(cast)
    assert root.saved() == []
    assert len(peer.saved()) == 1


def test_archive_survives_unsend(radio_pair: tuple[Radio, Radio]) -> None:
    """An archived copy outlives the original message (the copy's purpose)."""
    root, peer = radio_pair
    cast = peer.send(channel='outbox', subject='going', data='body', priority=5)
    root.save(cast)
    peer.unsend(cast)
    assert not root.db.exists('messages', where={'message_uuid': cast})
    [copy] = root.saved()
    assert copy['message_uuid'] == cast
    assert copy['data'] == 'body'


def test_unsave_not_found_stays_terse(radio: Radio) -> None:
    """Keep unsave's not-found terse -- it only ever reads the own archive."""
    with pytest.raises(ValueError, match='not found'):
        radio.unsave('DEADBEEF')


@pytest.mark.parametrize(
    'method', ['read', 'thread', 'reply', 'react', 'save', 'unsend']
)
def test_not_found_by_uuid(radio: Radio, method: str) -> None:
    """A not-found UUID raises a terse ValueError on every by-uuid command.

    UUIDs are globally unique in the central database, so an unresolvable one
    simply does not exist -- there is no other database to point at.
    """
    extra = {'reply': ('body',), 'react': (1,)}.get(method, ())
    call = getattr(radio, method)
    with pytest.raises(ValueError, match='not found'):
        call('DEADBEEF', *extra)


def test_unsend_deletes_message(radio: Radio) -> None:
    """Unsend removes a message from the database."""
    uuid = radio.send(subject='s', data='d', priority=0)
    assert len(radio.messages()) == 1
    radio.unsend(uuid)
    assert len(radio.messages()) == 0


def test_unsend_deletes_replies_and_reacts(radio: Radio) -> None:
    """Forced unsend cascades to replies, reacts, and reads."""
    uuid = radio.send(subject='root', data='d', priority=0)
    # add a reply and a react
    radio.reply(uuid, 'reply data')
    radio.react(uuid, 1)
    radio.read(uuid)
    assert len(radio.db.read('messages')) == 2
    assert len(radio.db.read('reacts')) == 1
    # --force unsends the whole thread, cascading everything
    radio.unsend(uuid, force=True)
    assert len(radio.db.read('messages')) == 0
    assert len(radio.db.read('reacts')) == 0
    assert len(radio.db.read('reads')) == 0


def test_unsend_refuses_thread_without_force(radio: Radio) -> None:
    """Unsend refuses a message with replies unless forced."""
    uuid = radio.send(subject='root', data='d', priority=0)
    radio.reply(uuid, 'reply data')
    # a message with replies is refused, leaving the thread intact
    with pytest.raises(ValueError, match='has replies'):
        radio.unsend(uuid)
    assert len(radio.db.read('messages')) == 2


def test_unsend_wrong_sender(radio_pair: tuple[Radio, Radio]) -> None:
    """Unsend rejects if caller is not the sender."""
    root, peer = radio_pair
    uuid = root.send(peer.node._branch, subject='s', data='d', priority=0)
    with pytest.raises(PermissionError, match='sender can unsend'):
        peer.unsend(uuid)


def test_unsend_force_aborts_on_concurrent_reply(radio: Radio) -> None:
    """Abort an unsend(force) when a reply lands between the walk and delete.

    The descendant set is re-collected immediately before the cascade; a reply
    that arrived in between would be re-rooted into an orphan, so unsend refuses
    and leaves the thread intact for a retry.
    """
    # a message to unsend, with a hook to inject a racing reply mid-walk
    uuid = radio.send(channel='public', subject='s', data='d', priority=0)
    original = radio._collect_message_ids
    calls = {'n': 0}

    def injecting(message_id: int, result: list) -> None:
        calls['n'] += 1
        original(message_id, result)
        if calls['n'] == 1:  # a reply lands after the first walk
            radio.reply(uuid, 'late')

    with patch.object(radio, '_collect_message_ids', side_effect=injecting):
        with pytest.raises(ValueError, match='mid-unsend'):
            radio.unsend(uuid, force=True)
    # the thread survived -- the abort happened before any delete
    assert radio.db.exists('messages', where={'message_uuid': uuid})
