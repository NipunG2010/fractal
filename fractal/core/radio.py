"""Implements ``Radio`` class."""

from __future__ import annotations

import secrets
from typing import Optional

from .db import Database
from .node import Node

__all__ = ['Radio']

_DEFAULT_CHANNELS = [
    {'channel': 'public', 'read_only': 0, 'write_only': 0},
    {'channel': 'private', 'read_only': 1, 'write_only': 1},
    {'channel': 'inbox', 'read_only': 1, 'write_only': 0},
    {'channel': 'outbox', 'read_only': 0, 'write_only': 1},
]


class Radio:
    """Inter-node messaging for autonomous agent nodes.

    Provides message sending, channels, subscriptions, threads,
    reactions, and read tracking in the tree's central database.
    """

    def __init__(self: Radio, node: Node) -> None:
        """Initialize ``Radio``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Radio) -> Node:
        """Owning node."""
        return self._node

    @property
    def db(self: Radio) -> Database:
        """Central database."""
        return self._node.db

    def init(self: Radio) -> None:
        """Seed default channels and auto-subscribe to parent/children.

        Called from ``Node.init`` once the node's config is written. Does:

        1. Insert default channels.
        2. Derive the parent from the branch name; subscribe
           to its readable channels.
        3. Query the ``nodes`` table for direct children;
           subscribe to each child's readable channels.
        """
        # seed default channels
        db = self.db
        branch = self.node._branch
        for channel in _DEFAULT_CHANNELS:
            db.merge({'node': branch, **channel}, 'channels')
        # subscribe to the parent's readable channels
        parts = branch.split('.')
        if len(parts) > 1:
            parent_branch = '.'.join(parts[:-1])
            self.subscribe(parent_branch)
        # subscribe to each direct child's readable channels (the registry is
        # tree-wide, so match on the subtree prefix, not depth alone)
        prefix = f'{branch}.'
        current_depth = branch.count('.')
        for row in db.read('nodes'):
            child_branch = row['node']
            if not child_branch.startswith(prefix):
                continue
            if child_branch.count('.') == current_depth + 1:
                self.subscribe(child_branch)

    def send(
        self: Radio,
        node: Optional[str] = None,
        channel: str = 'inbox',
        *,
        parent: bool = False,
        subject: str,
        data: str,
        priority: int,
    ) -> str:
        """Write a message to a node's channel.

        The message lands in the target's channel-space (the row's
        ``node`` column names the host). Checks ``write_only``
        permission and rejects if ``write_only = 1`` and the writer
        is not the owner.

        The permission check is best-effort, not atomic across connections: the
        ``write_only`` read and the ``db.write`` use separate connections, so a
        concurrent ``channel_delete`` of the target channel can race between them.
        Strict atomicity is intentionally not provided.

        Args:
            node: Target node branch. Defaults to self.
            channel: Channel name. Defaults to ``'inbox'``.
            parent: Send to the parent node (mutex with ``node``).
            subject: Message subject.
            data: Message data.
            priority: Priority (0-10).

        Returns:
            8-char message UUID.

        Raises:
            ValueError: If ``parent`` and ``node`` are both set, the
                parent or target node is not found, ``priority`` is out
                of range, or the channel is not found.
            PermissionError: If the target channel is write-only and the
                sender is not the owner.

        """
        # validate mutex
        if parent and node:
            raise ValueError('--parent and --node are mutually exclusive.')
        # resolve parent branch
        if parent:
            branch = self._node._branch
            parts = branch.rsplit('.', 1)
            if len(parts) < 2:
                raise ValueError('No parent node (top-level branch).')
            node = parts[0]
        # validate priority
        if not 0 <= priority <= 10:
            raise ValueError(f'Priority must be 0-10, got {priority}.')
        # resolve the target and validate the channel on it
        target = self._resolve_target(node)
        rows = self.db.read(
            'channels',
            where={'node': target, 'channel': channel},
            limit=1,
        )
        if not rows:
            raise ValueError(f'Channel not found: {channel!r}')
        if target != self.node._branch and rows[0]['write_only']:
            raise PermissionError(f'Channel {channel!r} is write-only (owner only).')
        # write message
        message_uuid = self._unique_uuid()
        session = self._sender_session()
        row = {
            'node': target,
            'message_uuid': message_uuid,
            'channel': channel,
            'sender': self.node._branch,
            'session': session,
            'priority': priority,
            'subject': subject,
            'data': data,
        }
        self.db.write(row, 'messages')
        return message_uuid

    def unsend(
        self: Radio,
        message_uuid: str,
        *,
        force: bool = False,
    ) -> None:
        """Delete a sent message and its replies.

        Only the sender can unsend. A message with replies is refused
        unless ``force`` is set, since deleting a thread also removes
        other nodes' replies. With ``force``, cascades to replies,
        reacts, and read receipts (the schema has no ``ON DELETE
        CASCADE``).

        The cascade is best-effort, not atomic across connections: each
        ``db.delete`` opens its own connection, so a reply that arrives mid-cascade
        from another node can race it (the pre-delete re-check narrows but does not
        close the window). Strict atomicity is intentionally not provided.

        Args:
            message_uuid: 8-char message UUID.
            force: Delete the whole thread even if it has replies.

        Raises:
            ValueError: If the message is not found, or it has replies
                and ``force`` is not set.
            PermissionError: If caller is not the sender.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # find message by UUID (globally unique)
        db = self.db
        rows = db.read('messages', where={'message_uuid': message_uuid}, limit=1)
        if rows:
            message = rows[0]
        else:
            raise self._message_not_found(message_uuid)
        # verify sender
        if message['sender'] != self.node._branch:
            raise PermissionError('Only the sender can unsend.')
        # collect all message IDs in the tree (root + replies)
        message_ids = []
        self._collect_message_ids(message['message_id'], message_ids)
        # refuse to delete a thread unless forced -- the cascade
        # also removes replies authored by other nodes
        if len(message_ids) > 1 and not force:
            raise ValueError(
                f'Message {message_uuid} has replies;'
                ' use --force to delete the whole thread.'
            )
        # re-collect immediately before deleting: a reply that arrived after the
        # first walk would otherwise be re-rooted into an orphan by the cascade
        re_check = []
        self._collect_message_ids(message['message_id'], re_check)
        if len(re_check) > len(message_ids):
            raise ValueError(
                f'Message {message_uuid} gained a reply mid-unsend;'
                ' retry to delete the whole thread.'
            )
        # cascade delete reacts, reads, then messages
        for mid in message_ids:
            db.delete('reacts', where={'message_id': mid})
            db.delete('reads', where={'message_id': mid})
            db.delete('messages', where={'message_id': mid})

    def messages(
        self: Radio,
        *,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
    ) -> list[dict]:
        """Query this node's messages with reply/react counts.

        Listing is always passive -- it never writes read receipts, so the
        unread view is stable across calls; ``read`` is the receipt-writing
        surface.

        Args:
            channel: Filter by channel name.
            limit: Maximum rows to return.
            since: Only messages after this ISO 8601 timestamp.
            read: ``None`` = all, ``False`` = unread only,
                ``True`` = read only.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            List of message dicts with ``replies``, ``pos_reacts``,
            and ``neg_reacts`` counts.

        """
        return self._query_messages(
            node=self.node._branch,
            channel=channel,
            limit=limit,
            since=since,
            read=read,
            recent=recent,
        )

    def sent(
        self: Radio,
        *,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        recent: bool = False,
    ) -> list[dict]:
        """Query messages this node sent, across every host's channel-space.

        Each row's ``node`` column names the recipient (the channel host).
        Replies list first-class -- unlike the mailbox listings, an authored
        reply that threads in place is not hidden behind its parent's reply
        count.

        Args:
            channel: Filter by channel name.
            limit: Maximum rows to return.
            since: Only messages after this ISO 8601 timestamp.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            List of message dicts with ``replies``, ``pos_reacts``,
            and ``neg_reacts`` counts.

        """
        return self._query_messages(
            sender=self.node._branch,
            channel=channel,
            limit=limit,
            since=since,
            recent=recent,
            roots_only=False,
        )

    def feed(
        self: Radio,
        *,
        node: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
    ) -> list[dict]:
        """Fan-out read across subscriptions.

        Reads this node's subs, optionally filtered by target node and/or
        channel. For each matching subscription, queries the target's
        channel where ``read_only = 0``. Merges and re-sorts by priority
        DESC / created_at ASC. Each row's ``node`` column names its
        source. Listing is always passive -- it never writes read
        receipts, so the unread view is stable across calls;
        ``read(feed=True)`` is the receipt-writing surface.

        Args:
            node: Filter subscriptions by target node.
            channel: Filter subscriptions by channel.
            limit: Maximum total rows to return.
            since: Only messages after this ISO 8601 timestamp.
            read: ``None`` = all, ``False`` = unread only,
                ``True`` = read only.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            Merged and sorted list of message dicts.

        """
        # read subscriptions
        reader = self.node._branch
        where = {'node': reader}
        if node is not None:
            where['target'] = node
        if channel is not None:
            where['channel'] = channel
        subs = self.db.read('subs', where=where)
        # fan-out across subscriptions
        messages = []
        for sub in subs:
            # check channel is readable on the target
            rows = self.db.read(
                'channels',
                where={'node': sub['target'], 'channel': sub['channel']},
                limit=1,
            )
            if not rows or rows[0]['read_only']:
                continue
            # query messages
            messages += self._query_messages(
                node=sub['target'],
                channel=sub['channel'],
                limit=None,
                since=since,
                read=read,
                recent=recent,
            )
        # re-sort merged results
        if recent:
            messages.sort(key=lambda m: m['created_at'], reverse=True)
        else:
            messages.sort(key=lambda m: (-m['priority'], m['created_at']))
        if limit is not None:
            messages = messages[:limit]
        # return rows
        return messages

    def read(
        self: Radio,
        *message_uuids: str,
        node: Optional[str] = None,
        channel: Optional[str] = None,
        feed: bool = False,
        unread: bool = False,
    ) -> list[dict]:
        """Read messages by UUID and/or selector, receipting each as this node.

        The body surface: reading is the act that consumes unread state
        (listing never does), and every returned row lands this node's
        receipt in the ``reads`` table -- receipts always attribute to
        the actual reader, never to the mailbox viewed. UUIDs resolve
        globally (unique across the tree) and return in argument order;
        selector rows follow priority DESC / created_at ASC and mirror
        the listings' row sets (thread roots plus cross-space replies);
        duplicates collapse to their first occurrence. Receipts land
        only after every lookup resolves, so a failed UUID receipts
        nothing.

        Args:
            *message_uuids: 8-char message UUIDs.
            node: Mailbox node branch the selectors view. Defaults to self.
            channel: Read this channel's messages.
            feed: Read messages from the subscribed nodes.
            unread: Restrict the selectors to rows this reader has no
                receipt for (explicit UUIDs always return).

        Returns:
            Full message dicts with data and reply/react counts.

        Raises:
            ValueError: If a message or the mailbox node is not found.
            PermissionError: If a channel is read-only and the reader
                is not the owner.

        """
        db = self.db
        messages = []
        # resolve explicit uuids globally, in argument order
        for message_uuid in message_uuids:
            message_uuid = message_uuid.upper()
            rows = db.read('messages', where={'message_uuid': message_uuid}, limit=1)
            if rows:
                message = rows[0]
            else:
                raise self._message_not_found(message_uuid)
            # reject a non-owner reaching a read-only channel
            self._reject_read_only(message['channel'], message['node'])
            # add reply/react counts (selector rows already carry them)
            message['replies'] = db.count(
                'messages',
                where={'parent_message_id': message['message_id']},
            )
            message['pos_reacts'] = db.count(
                'reacts',
                where={'message_id': message['message_id'], 'value': 1},
            )
            message['neg_reacts'] = db.count(
                'reacts',
                where={'message_id': message['message_id'], 'value': -1},
            )
            messages.append(message)
        # the unread filter follows this reader's receipts
        read = False if unread else None
        # expand the channel selector over the mailbox's channel-space
        if channel is not None:
            mailbox = self._resolve_target(node)
            self._reject_read_only(channel, mailbox)
            messages += self._query_messages(
                node=mailbox,
                channel=channel,
                read=read,
            )
        # expand the feed selector across the mailbox's subscriptions
        if feed:
            mailbox = self._resolve_target(node)
            fanned = []
            for sub in db.read('subs', where={'node': mailbox}):
                # check channel is readable on the target (mirrors feed)
                rows = db.read(
                    'channels',
                    where={'node': sub['target'], 'channel': sub['channel']},
                    limit=1,
                )
                if not rows or rows[0]['read_only']:
                    continue
                fanned += self._query_messages(
                    node=sub['target'],
                    channel=sub['channel'],
                    read=read,
                )
            fanned.sort(key=lambda m: (-m['priority'], m['created_at']))
            messages += fanned
        # collapse duplicates (a uuid may also match a selector), keeping first
        result = []
        seen = set()
        for message in messages:
            if message['message_id'] in seen:
                continue
            seen.add(message['message_id'])
            result.append(message)
        # write this reader's receipt for exactly the returned rows
        for message in result:
            receipt = {
                'message_id': message['message_id'],
                'node': self.node._branch,
                'channel': message['channel'],
            }
            db.merge(receipt, 'reads')
        return result

    def thread(
        self: Radio,
        message_uuid: str,
        *,
        read: Optional[bool] = None,
    ) -> list[dict]:
        """Full reply tree for a message.

        Finds root message (walks up ``parent_message_id``), collects
        all replies recursively. Returns flat list ordered by
        ``created_at`` ASC with ``depth`` field for indentation.

        Args:
            message_uuid: 8-char message UUID.
            read: ``None`` = all, ``False`` = unread only,
                ``True`` = read only.

        Returns:
            Flat list of message dicts with ``depth`` field.

        Raises:
            ValueError: If the message is not found.
            PermissionError: If the channel is read-only and the reader
                is neither the owner nor a thread participant.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # find message by UUID
        db = self.db
        rows = db.read('messages', where={'message_uuid': message_uuid}, limit=1)
        if rows:
            message = rows[0]
        else:
            raise self._message_not_found(message_uuid)
        # walk up to root
        root = message
        while root['parent_message_id'] is not None:
            parents = db.read(
                'messages',
                where={'message_id': root['parent_message_id']},
                limit=1,
            )
            if parents:
                root = parents[0]
            else:
                break
        # collect the full tree first -- the permission decision below needs
        # every participant, and the read filter applies only afterwards so a
        # matching descendant under a filtered-out ancestor is not pruned
        result = []
        self._collect_thread(root, 0, result)
        # reject a non-owner reaching a read-only channel -- thread participants
        # are exempt, so a rerouted conversation reads whole for both parties
        branch = self.node._branch
        participant = any(
            row['sender'] == branch or row['node'] == branch for row in result
        )
        if not participant:
            self._reject_read_only(message['channel'], message['node'])
        # apply the read filter to each row (following the caller's receipts)
        if read is not None:
            kept = []
            for row in result:
                receipts = db.read(
                    'reads',
                    where={'message_id': row['message_id'], 'node': branch},
                    limit=1,
                )
                include = bool(receipts) if read else not receipts
                if include:
                    kept.append(row)
            result = kept
        return result

    def reply(
        self: Radio,
        message_uuid: str,
        data: str,
        *,
        priority: Optional[int] = None,
    ) -> tuple[str, str, str]:
        """Reply to a message.

        Locates the parent by UUID (globally unique) and routes by where
        the parent sits: a reply to a message in the caller's own inbox
        is a conversation turn, so it lands in the original sender's
        inbox; so does a reply into another node's write-only channel --
        the reaction to a broadcast reaches its author's inbox rather
        than piercing the owner-only channel; any other reply is
        created in the parent's host channel-space. ``thread``
        walks parent ids regardless of host, so a rerouted conversation
        still renders as one thread. Inherits subject (``Re: ...``) and
        priority from the parent if not specified. Also marks the parent
        read (like ``react``) so a replied-to message does not resurface
        every SYNC.

        The channel-flag probe is best-effort, not atomic across
        connections: the channel read and the ``db.write`` use separate
        connections, so a concurrent ``channel_delete`` or ``unsend`` of the
        parent can race between them. Strict atomicity is intentionally not
        provided.

        Args:
            message_uuid: 8-char UUID of the parent message.
            data: Reply data.
            priority: Reply priority (0-10). Inherited if omitted.

        Returns:
            Tuple of the reply's 8-char message UUID and the resolved
            destination ``(node, channel)``, so callers can surface the
            routing the way ``send`` does.

        Raises:
            ValueError: If the parent message is not found, or
                ``priority`` is out of range.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # find parent
        db = self.db
        rows = db.read('messages', where={'message_uuid': message_uuid}, limit=1)
        if rows:
            parent = rows[0]
        else:
            raise self._message_not_found(message_uuid)
        # probe the parent's channel flags (write-only reroutes the reply below)
        write_only = False
        if parent['node'] != self.node._branch:
            rows = db.read(
                'channels',
                where={'node': parent['node'], 'channel': parent['channel']},
                limit=1,
            )
            write_only = bool(rows and rows[0]['write_only'])
        # inherit subject and priority, normalizing any existing reply prefix to
        # a single canonical 'Re: ' (a parent 'RE: x' / 're: x' becomes 'Re: x')
        subject = parent['subject']
        if subject.lower().startswith('re: '):
            subject = subject[len('Re: ') :]
        subject = f'Re: {subject}'
        if priority is None:
            priority = parent['priority']
        if not 0 <= priority <= 10:
            raise ValueError(f'Priority must be 0-10, got {priority}.')
        # route the reply: a turn in my own inbox or into a write-only channel
        # goes back to the sender's inbox; anything else threads in place
        own_inbox = parent['node'] == self.node._branch and parent['channel'] == 'inbox'
        if own_inbox or write_only:
            node = parent['sender']
            channel = 'inbox'
        else:
            node = parent['node']
            channel = parent['channel']
        # create the reply
        reply_uuid = self._unique_uuid()
        session = self._sender_session()
        row = {
            'node': node,
            'message_uuid': reply_uuid,
            'parent_message_id': parent['message_id'],
            'parent_message_uuid': parent['message_uuid'],
            'channel': channel,
            'sender': self.node._branch,
            'session': session,
            'priority': priority,
            'subject': subject,
            'data': data,
        }
        db.write(row, 'messages')
        # also mark the parent read so replying clears it from SYNC's inbox/feed
        # -- otherwise a replied-to message resurfaces every sync (mirror react())
        receipt = {
            'message_id': parent['message_id'],
            'node': self.node._branch,
            'channel': parent['channel'],
        }
        db.merge(receipt, 'reads')
        return reply_uuid, node, channel

    def react(
        self: Radio,
        message_uuid: str,
        value: int,
    ) -> None:
        """React to a message with 1 or -1.

        Locates the message by UUID (globally unique) and upserts into
        ``reacts``, so re-reacting changes the value. Also marks the
        message read (a ``reads`` receipt), mirroring ``read``, so an
        ack clears it from SYNC.

        Args:
            message_uuid: 8-char message UUID.
            value: Reaction value (``1`` or ``-1``).

        Raises:
            ValueError: If ``value`` is not ``1`` or ``-1``, or the
                message is not found.
            PermissionError: If the channel is read-only and the reactor
                is not the owner.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # validate value
        if value not in (1, -1):
            raise ValueError('Value must be 1 or -1.')
        # find message by UUID
        db = self.db
        rows = db.read('messages', where={'message_uuid': message_uuid}, limit=1)
        if rows:
            message = rows[0]
        else:
            raise self._message_not_found(message_uuid)
        # reject a non-owner reaching a read-only channel
        self._reject_read_only(message['channel'], message['node'])
        # write react
        data = {
            'message_id': message['message_id'],
            'node': self.node._branch,
            'channel': message['channel'],
            'value': value,
        }
        db.merge(data, 'reacts')
        # also mark the message read so reacting clears it from SYNC's inbox/feed
        # (otherwise the same items resurface every sync, burning tokens)
        receipt = {
            'message_id': message['message_id'],
            'node': self.node._branch,
            'channel': message['channel'],
        }
        db.merge(receipt, 'reads')

    def save(
        self: Radio,
        message_uuid: str,
    ) -> None:
        """Save a message to the archive.

        Locates the message by UUID (globally unique) and copies it into
        the ``archive`` table -- an owned snapshot that survives unsend.
        Re-saving is idempotent.

        Args:
            message_uuid: 8-char message UUID.

        Raises:
            ValueError: If the message is not found.
            PermissionError: If the channel is read-only and the saver
                is not the owner.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # find message by UUID
        db = self.db
        rows = db.read('messages', where={'message_uuid': message_uuid}, limit=1)
        if rows:
            message = rows[0]
        else:
            raise self._message_not_found(message_uuid)
        # reject a non-owner reaching a read-only channel
        self._reject_read_only(message['channel'], message['node'])
        # copy into the archive (node = saver, owner = the message's host)
        row = {
            'node': self.node._branch,
            'message_id': message['message_id'],
            'message_uuid': message['message_uuid'],
            'parent_message_id': message.get('parent_message_id'),
            'parent_message_uuid': message.get('parent_message_uuid'),
            'channel': message['channel'],
            'sender': message['sender'],
            'session': message.get('session'),
            'owner': message['node'],
            'priority': message['priority'],
            'subject': message['subject'],
            'data': message['data'],
            'metadata': message.get('metadata', ''),
            'created_at': message['created_at'],
        }
        db.merge(row, 'archive')

    def unsave(
        self: Radio,
        message_uuid: str,
    ) -> None:
        """Remove a message from the archive.

        Args:
            message_uuid: 8-char message UUID.

        Raises:
            ValueError: If this node has no archived copy of the message.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # the archive keys on (node, uuid); scope the lookup to this saver
        where = {'node': self.node._branch, 'message_uuid': message_uuid}
        # find the archived message
        if not self.db.exists('archive', where=where):
            raise ValueError(f'Message not found: {message_uuid!r}')
        # delete from archive
        self.db.delete('archive', where=where)

    def saved(
        self: Radio,
        *,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        recent: bool = False,
    ) -> list[dict]:
        """List saved messages from the archive.

        Args:
            limit: Maximum rows to return.
            since: Only messages saved after this ISO 8601
                timestamp.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            List of archived message dicts.

        """
        # build query
        conditions = ['node = ?']
        params = [self.node._branch]
        if since is not None:
            conditions.append('created_at > ?')
            params.append(since)
        query = 'SELECT * FROM archive'
        query += ' WHERE ' + ' AND '.join(conditions)
        if recent:
            query += ' ORDER BY created_at DESC'
        else:
            query += ' ORDER BY priority DESC, created_at ASC'
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)
        # execute query
        return self.db.read(query=query, params=tuple(params))

    def channel(
        self: Radio,
        name: str,
        *,
        read_only: bool = False,
        write_only: bool = False,
    ) -> None:
        """Register a custom channel.

        Defaults to public (both ``False`` = everyone can read
        and write). Rejects default channel names as reserved,
        and an already-registered channel name as a duplicate --
        create never silently overwrites an existing channel's
        flags (use ``channel_delete`` then recreate to change them).

        Args:
            name: Channel name.
            read_only: Only owner can read.
            write_only: Only owner can write.

        Raises:
            ValueError: If the channel is reserved or already exists.

        """
        # check reserved names
        reserved = {channel['channel'] for channel in _DEFAULT_CHANNELS}
        if name in reserved:
            raise ValueError(f'Channel name is reserved: {name!r}')
        # reject a duplicate -- a whole-row replace resets flags and mints a new
        # channel_id, silently downgrading e.g. a --read-only channel
        branch = self.node._branch
        if self.db.exists('channels', where={'node': branch, 'channel': name}):
            raise ValueError(f'Channel already exists: {name!r}')
        # write channel
        data = {
            'node': branch,
            'channel': name,
            'read_only': int(read_only),
            'write_only': int(write_only),
        }
        self.db.merge(data, 'channels')

    def channel_delete(self: Radio, name: str, *, force: bool = False) -> None:
        """Delete a custom channel and all of its data.

        Rejects default channel names as reserved. A channel that still
        holds messages is refused unless ``force`` is set, since the
        cascade also removes other nodes' replies (mirroring ``unsend``).
        With ``force``, cascades to the channel's messages, reacts, and
        read receipts (the schema has no ``ON DELETE CASCADE``) before
        removing its subscriptions and the channel row, so no orphaned
        message outlives the channel definition that gates its read-only
        access. Scoped to this node's channel-space -- another node's
        same-named channel is untouched.

        The cascade is best-effort, not atomic across connections: each
        ``db.delete`` opens its own connection, so a send/reply into the channel
        that races the cascade can leave a row behind the channel row's removal.
        Strict atomicity is intentionally not provided.

        Args:
            name: Channel name.
            force: Delete the channel even if it still holds messages.

        Raises:
            ValueError: If the channel is reserved, not found, or it
                holds messages and ``force`` is not set.

        """
        # check reserved names
        reserved = {channel['channel'] for channel in _DEFAULT_CHANNELS}
        if name in reserved:
            raise ValueError(f'Cannot delete default channel: {name!r}')
        branch = self.node._branch
        where = {'node': branch, 'channel': name}
        if not self.db.exists('channels', where=where):
            raise ValueError(f'Channel not found: {name!r}')
        # refuse to delete a channel that still holds messages unless forced --
        # the cascade also removes replies authored by other nodes
        count = self.db.count('messages', where=where)
        if count and not force:
            s = 's' if count != 1 else ''
            raise ValueError(
                f'Channel {name} has {count} message{s};'
                ' use --force to delete it and them.'
            )
        # cascade delete the channel's messages, receipts, subs, and channel
        # (the schema has no ON DELETE CASCADE) -- an orphaned message that
        # outlived its channel definition would no longer be gated as read-only
        for row in self.db.read('messages', where=where):
            self.db.delete('reacts', where={'message_id': row['message_id']})
            self.db.delete('reads', where={'message_id': row['message_id']})
        self.db.delete('messages', where=where)
        self.db.delete('subs', where={'target': branch, 'channel': name})
        self.db.delete('channels', where=where)

    def channels(self: Radio) -> list[dict]:
        """List this node's channels."""
        return self.db.read('channels', where={'node': self.node._branch})

    def subscribe(
        self: Radio,
        node: str,
        *,
        channel: Optional[str] = None,
    ) -> None:
        """Subscribe to a node's channel.

        If ``channel`` is omitted, subscribes to all readable
        (``read_only = 0``) channels on the target node.

        Args:
            node: Target node branch.
            channel: Channel name. All readable if omitted.

        """
        # resolve the target (validates it is registered)
        target = self._resolve_target(node)
        # subscribe to channel
        if channel is not None:
            # validate the channel exists and is readable on the target
            # (same rule the all-channels branch applies below)
            rows = self.db.read(
                'channels',
                where={'node': target, 'channel': channel},
                limit=1,
            )
            if not rows:
                raise ValueError(f'Channel not found on {target!r}: {channel!r}')
            if rows[0]['read_only']:
                raise ValueError(
                    f'Channel {channel!r} on {target!r} cannot be subscribed.'
                )
            data = {'node': self.node._branch, 'target': target, 'channel': channel}
            self.db.merge(data, 'subs')
        else:
            # subscribe to all readable channels on target
            for row in self.db.read('channels', where={'node': target}):
                if not row['read_only']:
                    data = {
                        'node': self.node._branch,
                        'target': target,
                        'channel': row['channel'],
                    }
                    self.db.merge(data, 'subs')

    def unsubscribe(
        self: Radio,
        node: str,
        *,
        channel: Optional[str] = None,
    ) -> None:
        """Unsubscribe from a node's channel.

        If ``channel`` is omitted, removes all subscriptions
        to the target node.

        Args:
            node: Target node branch.
            channel: Channel name. All if omitted.

        """
        where = {'node': self.node._branch, 'target': node}
        if channel is not None:
            where['channel'] = channel
        self.db.delete('subs', where=where)

    def subs(self: Radio) -> list[dict]:
        """List this node's subscriptions."""
        return self.db.read('subs', where={'node': self.node._branch})

    def _resolve_target(self: Radio, node: Optional[str]) -> str:
        """Normalize a target to a branch name, defaulting to the own node.

        ``None``, the empty string, and the own branch all resolve to self.
        Any other target must be the tree's root or have a ``nodes`` registry
        row -- a deleted node's persisted rows do not make it addressable.

        Args:
            node: Target node branch, or ``None``/``''`` for the own node.

        Returns:
            The resolved branch name.

        Raises:
            ValueError: If ``node`` names a node that is not registered.

        """
        branch = self.node._branch
        if not node or node == branch:
            return branch
        root = self.node.config_get('root')
        if node != root and not self.db.exists('nodes', where={'node': node}):
            raise ValueError(f'Node not found: {node!r}')
        return node

    def _query_messages(
        self: Radio,
        *,
        node: Optional[str] = None,
        sender: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
        roots_only: bool = True,
    ) -> list[dict]:
        """Build and execute a messages query with counts.

        Args:
            node: Filter by host node (whose channel-space).
            sender: Filter by sending node.
            channel: Filter by channel.
            limit: Maximum rows.
            since: Only after this timestamp.
            read: Read filter, following this node's receipts.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.
            roots_only: Keep thread roots (and cross-space replies) only.

        Returns:
            List of message dicts with counts.

        """
        # build WHERE clauses -- mailbox views keep thread roots, plus any reply
        # routed across channel-spaces (a rerouted conversation turn is new
        # mail for its host; in-place replies stay behind the parent's count)
        conditions = []
        params = []
        if roots_only:
            conditions.append(
                '(m.parent_message_id IS NULL OR NOT EXISTS'
                ' (SELECT 1 FROM messages p'
                ' WHERE p.message_id = m.parent_message_id'
                ' AND p.node = m.node AND p.channel = m.channel))'
            )
        if node is not None:
            conditions.append('m.node = ?')
            params.append(node)
        if sender is not None:
            conditions.append('m.sender = ?')
            params.append(sender)
        if channel is not None:
            conditions.append('m.channel = ?')
            params.append(channel)
        if since is not None:
            conditions.append('m.created_at > ?')
            params.append(since)
        if read is not None:
            # read state follows the caller's receipt in reads
            exists = (
                'EXISTS (SELECT 1 FROM reads r'
                ' WHERE r.message_id = m.message_id AND r.node = ?)'
            )
            conditions.append(exists if read else f'NOT {exists}')
            params.append(self.node._branch)
        where_clause = ' AND '.join(conditions)
        # build query
        query = (
            'SELECT m.*,'
            '  (SELECT COUNT(*) FROM messages r'
            '   WHERE r.parent_message_id = m.message_id) AS replies,'
            '  (SELECT COUNT(*) FROM reacts r'
            '   WHERE r.message_id = m.message_id AND r.value = 1) AS pos_reacts,'
            '  (SELECT COUNT(*) FROM reacts r'
            '   WHERE r.message_id = m.message_id AND r.value = -1) AS neg_reacts'
            f' FROM messages m'
            f' WHERE {where_clause}'
        )
        if recent:
            order = 'm.created_at DESC'
        else:
            order = 'm.priority DESC, m.created_at ASC'
        query += f' ORDER BY {order}'
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)
        # execute query
        return self.db.read(query=query, params=tuple(params))

    def _collect_thread(
        self: Radio,
        message: dict,
        depth: int,
        result: list[dict],
    ) -> None:
        """Recursively collect a thread tree into a flat list.

        Args:
            message: Current message dict.
            depth: Current nesting depth.
            result: Accumulator list (mutated in place).

        """
        message['depth'] = depth
        result.append(message)
        # find children
        children = self.db.read(
            'messages',
            where={'parent_message_id': message['message_id']},
        )
        # re-sort by created_at ASC (db.read defaults to DESC)
        children.sort(key=lambda m: m['created_at'])
        for child in children:
            self._collect_thread(child, depth + 1, result)

    def _collect_message_ids(
        self: Radio,
        message_id: int,
        result: list[int],
    ) -> None:
        """Collect a message ID and all descendant reply IDs.

        Args:
            message_id: Root message ID.
            result: Accumulator list (mutated in place).

        """
        result.append(message_id)
        children = self.db.read('messages', where={'parent_message_id': message_id})
        for child in children:
            self._collect_message_ids(child['message_id'], result)

    def _reject_read_only(self: Radio, channel: str, host: str) -> None:
        """Reject a non-owner acting on a read-only channel (owner only).

        Shared by ``read``/``thread``/``react``/``save`` so a cross-node caller
        cannot reach a read-only channel's contents or receipts.

        Args:
            channel: Channel name to check.
            host: The channel's owning node (the message's ``node`` column).

        Raises:
            PermissionError: If ``host`` is a different node and the channel is
                read-only.

        Note:
            A missing channel row is allowed, not rejected: under the central
            database every channel is co-located, so an absent row means the
            channel does not exist -- there is nothing to gate, and the caller's
            own operation surfaces the not-found error.

        """
        if host != self.node._branch:
            rows = self.db.read(
                'channels',
                where={'node': host, 'channel': channel},
                limit=1,
            )
            if rows and rows[0]['read_only']:
                raise PermissionError(f'Channel {channel!r} is read-only (owner only).')

    def _message_not_found(self: Radio, message_uuid: str) -> ValueError:
        """Build the by-UUID not-found error."""
        return ValueError(f'Message not found: {message_uuid!r}')

    def _sender_session(self: Radio) -> Optional[str]:
        """Return the acting node's live agent session, stamped on sends.

        The session ties a message to the conversation that wrote it (the
        cockpit's "chat about this" forks it). ``None`` when the node has no
        configured agent or no session woven this iteration.
        """
        if agent := self.node._default_agent():
            return self.node.session_get(agent)
        return None

    def _generate_uuid(self: Radio) -> str:
        """Generate an 8-char uppercase hex UUID."""
        return secrets.token_hex(4).upper()

    def _unique_uuid(self: Radio, max_retries: int = 10) -> str:
        """Generate a message UUID not already present in ``messages``.

        Args:
            max_retries: Maximum generation attempts before giving up.

        Returns:
            An 8-char UUID unique across the tree's ``messages`` table.

        Raises:
            RuntimeError: If no unique UUID is found within ``max_retries``
                attempts.

        """
        for _ in range(max_retries):
            message_uuid = self._generate_uuid()
            if not self.db.exists('messages', where={'message_uuid': message_uuid}):
                return message_uuid
        raise RuntimeError(
            f'Could not generate a unique message UUID after {max_retries} attempts.'
        )
