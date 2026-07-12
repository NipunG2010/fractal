"""Implements ``fractal radio`` sub-app commands."""

from __future__ import annotations

import sys
from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    print_rows,
    require_non_negative,
    resolve_node,
)
from fractal.core.node import Node
from fractal.core.radio import Radio

__all__ = [
    'radio_send',
    'radio_unsend',
    'radio_save',
    'radio_unsave',
    'radio_messages',
    'radio_sent',
    'radio_feed',
    'radio_read',
    'radio_thread',
    'radio_reply',
    'radio_react',
    'radio_sub',
    'radio_unsub',
    'radio_subs',
]

# empty-result headers must mirror the populated shapes exactly so parsers
# can key on one header per listing; the metadata listings (messages/feed)
# drop the data column -- `read` is the body surface
_MESSAGE_COLUMNS = [
    'message_id',
    'node',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'priority',
    'subject',
    'data',
    'metadata',
    'created_at',
    'replies',
    'pos_reacts',
    'neg_reacts',
]

_METADATA_COLUMNS = [
    'message_id',
    'node',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'priority',
    'subject',
    'metadata',
    'created_at',
    'replies',
    'pos_reacts',
    'neg_reacts',
]

_SAVED_COLUMNS = [
    'archive_id',
    'node',
    'message_id',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'owner',
    'priority',
    'subject',
    'data',
    'metadata',
    'created_at',
]

_THREAD_COLUMNS = [
    'message_id',
    'node',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'priority',
    'subject',
    'data',
    'metadata',
    'created_at',
    'depth',
]

_SUB_COLUMNS = [
    'sub_id',
    'node',
    'target',
    'channel',
    'created_at',
]


def radio_send(app: typer.Typer) -> typer.Typer:
    """Register the ``send`` command."""
    # data argument
    data_help = 'Message data.'
    data = typer.Argument(..., help=data_help)
    # node option
    node_help = 'Target node branch.'
    node = typer.Option(None, '--node', help=node_help)
    # parent flag
    parent_help = 'Send to parent node (mutex with --node).'
    parent = typer.Option(False, '--parent', help=parent_help)
    # channel option
    channel_help = (
        "Channel name (default: 'inbox' if target node specified, else your"
        " 'outbox'; private notes are an explicit --channel=private opt-in)."
    )
    channel = typer.Option(None, '--channel', help=channel_help)
    # subject option
    subject_help = 'Message subject.'
    subject = typer.Option(..., '--subject', help=subject_help)
    # priority option
    priority_help = 'Message priority (0-10).'
    priority = typer.Option(..., '--priority', help=priority_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'send')
    def _send(
        data: str = data,
        node: Optional[str] = node,
        parent: bool = parent,
        channel: Optional[str] = channel,
        subject: str = subject,
        priority: int = priority,
        path: str = path,
    ) -> None:
        """Send a message to a node's channel."""
        # reporting out is the common case, so a bare send defaults to the
        # sender's own outbox -- a private default would make doc-following
        # status reports vanish
        if channel is None:
            channel = 'inbox' if (node or parent) else 'outbox'
        radio = Radio(resolve_node(path))
        message_uuid = radio.send(
            node=node,
            channel=channel,
            parent=parent,
            subject=subject,
            data=data,
            priority=priority,
        )
        typer.echo(message_uuid)
        # echo the resolved routing so a misdelivered send is visible
        # immediately -- on stderr (stdout stays the bare UUID for scripts)
        # and unconditionally, since the misdelivery victims are agents, not
        # interactive TTY users
        branch = radio.node._branch
        if parent:
            target = branch.rsplit('.', 1)[0]
        elif node:
            target = node
        else:
            target = branch
        typer.echo(f"sent to {target}'s {channel!r} channel", err=True)

    return app


def radio_unsend(app: typer.Typer) -> typer.Typer:
    """Register the ``unsend`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # force flag
    force_help = 'Delete the whole thread even if the message has replies.'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'unsend')
    def _unsend(
        message_uuid: str = message_uuid,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Delete a sent message (refused if it has replies; use --force)."""
        radio = Radio(resolve_node(path))
        radio.unsend(message_uuid, force=force)
        typer.echo(f'Unsent {message_uuid}.')

    return app


def radio_save(app: typer.Typer) -> typer.Typer:
    """Register the ``save`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'save')
    def _save(
        message_uuid: str = message_uuid,
        path: str = path,
    ) -> None:
        """Save a message to the archive."""
        radio = Radio(resolve_node(path))
        radio.save(message_uuid)
        typer.echo(f'Saved {message_uuid}.')

    return app


def radio_unsave(app: typer.Typer) -> typer.Typer:
    """Register the ``unsave`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'unsave')
    def _unsave(
        message_uuid: str = message_uuid,
        path: str = path,
    ) -> None:
        """Remove a message from the archive."""
        radio = Radio(resolve_node(path))
        radio.unsave(message_uuid)
        typer.echo(f'Unsaved {message_uuid}.')

    return app


def radio_messages(app: typer.Typer) -> typer.Typer:
    """Register the ``messages`` command."""
    # channel option
    channel_help = "Filter by channel name (default: 'inbox')."
    channel = typer.Option(None, '--channel', help=channel_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # since option
    since_help = 'Only messages after this timestamp.'
    since = typer.Option(None, '--since', help=since_help)
    # read messages option
    read_messages_help = 'Show only read messages (default: unread only).'
    read_messages = typer.Option(False, '--read', help=read_messages_help)
    # all messages option
    all_messages_help = 'Show all messages (default: unread only).'
    all_messages = typer.Option(False, '--all', help=all_messages_help)
    # saved messages option
    saved_messages_help = 'Show saved messages (mutex with --read/--all).'
    saved_messages = typer.Option(False, '--saved', help=saved_messages_help)
    # recent flag
    recent_help = 'Sort by most recent instead of priority.'
    recent = typer.Option(False, '--recent', help=recent_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'messages')
    def _messages(
        channel: Optional[str] = channel,
        limit: Optional[int] = limit,
        since: Optional[str] = since,
        read_messages: bool = read_messages,
        all_messages: bool = all_messages,
        saved_messages: bool = saved_messages,
        recent: bool = recent,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List a channel's metadata, inbox by default (bodies via 'read')."""
        require_non_negative(limit=limit)
        # resolve node
        radio = Radio(resolve_node(path))
        # --saved: show archived messages
        if saved_messages:
            if read_messages or all_messages:
                raise typer.BadParameter(
                    '--saved is mutually exclusive with --read/--all.'
                )
            rows = radio.saved(limit=limit, since=since, recent=recent)
            print_rows(rows, csv=csv, columns=_SAVED_COLUMNS)
            return
        read = _read_filter(all_messages, read_messages)
        # a bare `messages` (no --channel) shows only the inbox -- not all your
        # own channels -- so own outbox/private notes don't read as incoming mail;
        # hint interactive callers that the other channels exist (TTY only --
        # agents read bare `messages` every sync, and a per-call notice is noise)
        if channel is None:
            channel = 'inbox'
            if sys.stderr.isatty():
                typer.echo(
                    'no channel specified, defaulting to inbox '
                    '(use --channel=<channel> for your other channels)',
                    err=True,
                )
        rows = radio.messages(
            channel=channel,
            limit=limit,
            since=since,
            read=read,
            recent=recent,
        )
        # metadata-only listing: the body never rides it -- `read` is the
        # body surface
        rows = [{key: row[key] for key in _METADATA_COLUMNS} for row in rows]
        print_rows(rows, csv=csv, columns=_METADATA_COLUMNS)

    return app


def radio_sent(app: typer.Typer) -> typer.Typer:
    """Register the ``sent`` command."""
    # channel option
    channel_help = 'Filter by the recipient channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # since option
    since_help = 'Only messages after this timestamp.'
    since = typer.Option(None, '--since', help=since_help)
    # recent flag
    recent_help = 'Sort by most recent instead of priority.'
    recent = typer.Option(False, '--recent', help=recent_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'sent')
    def _sent(
        channel: Optional[str] = channel,
        limit: Optional[int] = limit,
        since: Optional[str] = since,
        recent: bool = recent,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List messages this node sent (the node column is the recipient)."""
        require_non_negative(limit=limit)
        radio = Radio(resolve_node(path))
        rows = radio.sent(
            channel=channel,
            limit=limit,
            since=since,
            recent=recent,
        )
        print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)

    return app


def radio_feed(app: typer.Typer) -> typer.Typer:
    """Register the ``feed`` command."""
    # node option
    node_help = 'Filter by node branch.'
    node = typer.Option(None, '--node', help=node_help)
    # channel option
    channel_help = 'Filter by channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # since option
    since_help = 'Only messages after this timestamp.'
    since = typer.Option(None, '--since', help=since_help)
    # read messages option
    read_messages_help = 'Show only read messages (default: unread only).'
    read_messages = typer.Option(False, '--read', help=read_messages_help)
    # all messages option
    all_messages_help = 'Show all messages (default: unread only).'
    all_messages = typer.Option(False, '--all', help=all_messages_help)
    # saved messages option
    saved_messages_help = 'Show saved messages (mutex with --read/--all).'
    saved_messages = typer.Option(False, '--saved', help=saved_messages_help)
    # recent flag
    recent_help = 'Sort by most recent instead of priority.'
    recent = typer.Option(False, '--recent', help=recent_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'feed')
    def _feed(
        node: Optional[str] = node,
        channel: Optional[str] = channel,
        limit: Optional[int] = limit,
        since: Optional[str] = since,
        read_messages: bool = read_messages,
        all_messages: bool = all_messages,
        saved_messages: bool = saved_messages,
        recent: bool = recent,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List subscribed nodes' metadata (bodies via 'read --feed')."""
        require_non_negative(limit=limit)
        # resolve node
        radio = Radio(resolve_node(path))
        # --saved: show archived messages
        if saved_messages:
            if read_messages or all_messages:
                raise typer.BadParameter(
                    '--saved is mutually exclusive with --read/--all.'
                )
            rows = radio.saved(limit=limit, since=since, recent=recent)
            print_rows(rows, csv=csv, columns=_SAVED_COLUMNS)
            return
        read = _read_filter(all_messages, read_messages)
        rows = radio.feed(
            node=node,
            channel=channel,
            limit=limit,
            since=since,
            read=read,
            recent=recent,
        )
        # metadata-only listing: the body never rides it -- `read` is the
        # body surface
        rows = [{key: row[key] for key in _METADATA_COLUMNS} for row in rows]
        print_rows(rows, csv=csv, columns=_METADATA_COLUMNS)

    return app


def radio_read(app: typer.Typer) -> typer.Typer:
    """Register the ``read`` command."""
    # message_uuids argument
    message_uuids_help = '8-char message UUIDs.'
    message_uuids = typer.Argument(None, help=message_uuids_help)
    # channel option
    channel_help = "Read this channel of the viewed mailbox's channel-space."
    channel = typer.Option(None, '--channel', help=channel_help)
    # feed flag
    feed_help = "Read messages from the viewed mailbox's subscribed nodes."
    feed = typer.Option(False, '--feed', help=feed_help)
    # unread flag
    unread_help = 'Only messages you have not read (requires --channel/--feed).'
    unread = typer.Option(False, '--unread', help=unread_help)
    # path option
    path_help = 'Worktree directory of the mailbox to view (defaults to your own).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'read')
    def _read(
        message_uuids: Optional[list[str]] = message_uuids,
        channel: Optional[str] = channel,
        feed: bool = feed,
        unread: bool = unread,
        path: Optional[str] = path,
    ) -> None:
        """Print full messages by UUID/selector, marking them read (as you)."""
        # validate the selector shape
        if not message_uuids and channel is None and not feed:
            raise typer.BadParameter('Pass message UUIDs, --channel, or --feed.')
        if unread and channel is None and not feed:
            raise typer.BadParameter('--unread requires --channel or --feed.')
        # the reader is who runs the command; --path only selects whose mailbox
        # is viewed, so receipts stay truthful
        reader = _resolve_reader()
        if path is None:
            mailbox = reader
        else:
            mailbox = resolve_node(path)
            # refuse a mailbox from another tree loudly: the reader's radio
            # resolves branch names against its own central DB, where a foreign
            # branch either collides with a same-named node (silently reading
            # -- and receipting -- the wrong mailbox) or resolves to nothing
            if mailbox.db._path != reader.db._path:
                raise typer.BadParameter(
                    '--path names a mailbox in a different fractal tree;'
                    ' read it as a node of that tree (run from one of its'
                    ' worktrees or export _NODE).'
                )
        radio = Radio(reader)
        messages = radio.read(
            *(message_uuids or []),
            node=mailbox._branch,
            channel=channel,
            feed=feed,
            unread=unread,
        )
        for index, message in enumerate(messages):
            if index:
                typer.echo('')
            uuid = message['message_uuid']
            sender = message['sender']
            node = message['node']
            timestamp = message['created_at']
            message_channel = message['channel']
            subject = message['subject']
            priority = message['priority']
            data = message['data']
            typer.echo(f'Message UUID: {uuid}')
            typer.echo(f'From: {sender}')
            typer.echo(f'Node: {node}')
            typer.echo(f'Timestamp: {timestamp}')
            typer.echo(f'Channel: {message_channel}')
            typer.echo(f'Subject: {subject}')
            typer.echo(f'Priority: {priority}')
            typer.echo('')
            typer.echo(data)

    return app


def radio_thread(app: typer.Typer) -> typer.Typer:
    """Register the ``thread`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'thread')
    def _thread(
        message_uuid: str = message_uuid,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """Show a message's full reply tree (root and all replies)."""
        radio = Radio(resolve_node(path))
        rows = radio.thread(message_uuid)
        if csv:
            print_rows(rows, csv=csv, columns=_THREAD_COLUMNS)
        else:
            for message in rows:
                indent = '  ' * message.get('depth', 0)
                uuid = message['message_uuid']
                sender = message['sender']
                timestamp = message['created_at']
                priority = message['priority']
                subject = message['subject']
                typer.echo(
                    f'{indent}[{uuid}] {sender}'
                    f' ({timestamp}, priority {priority}): {subject}'
                )

    return app


def radio_reply(app: typer.Typer) -> typer.Typer:
    """Register the ``reply`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # data argument
    data_help = 'Message data.'
    data = typer.Argument(..., help=data_help)
    # priority option
    priority_help = 'Message priority (0-10).'
    priority = typer.Option(None, '--priority', help=priority_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'reply')
    def _reply(
        message_uuid: str = message_uuid,
        data: str = data,
        priority: Optional[int] = priority,
        path: str = path,
    ) -> None:
        """Reply to a message."""
        radio = Radio(resolve_node(path))
        reply_uuid, target, channel = radio.reply(
            message_uuid,
            data,
            priority=priority,
        )
        typer.echo(reply_uuid)
        # echo the resolved routing on stderr, mirroring send -- routing is
        # least obvious exactly on replies, where the destination is derived,
        # not named
        typer.echo(f"sent to {target}'s {channel!r} channel", err=True)

    return app


def radio_react(app: typer.Typer) -> typer.Typer:
    """Register the ``react`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # value argument
    value_help = 'Reaction value (+ or -).'
    value = typer.Argument(..., help=value_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'react')
    def _react(
        message_uuid: str = message_uuid,
        value: str = value,
        path: str = path,
    ) -> None:
        """React to a message (+ or -)."""
        # convert +/- to 1/-1
        if value == '+':
            int_value = 1
        elif value == '-':
            int_value = -1
        else:
            raise typer.BadParameter("value must be '+' or '-'.")
        # resolve node
        radio = Radio(resolve_node(path))
        radio.react(message_uuid, int_value)
        typer.echo(f'Reacted {value} to {message_uuid}.')

    return app


def radio_sub(app: typer.Typer) -> typer.Typer:
    """Register the ``sub`` command."""
    # node option
    node_help = 'Target node branch.'
    node = typer.Option(..., '--node', help=node_help)
    # channel option
    channel_help = 'Filter by channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'sub')
    def _sub(
        node: str = node,
        channel: Optional[str] = channel,
        path: str = path,
    ) -> None:
        """Subscribe to a node's channel."""
        radio = Radio(resolve_node(path))
        radio.subscribe(node, channel=channel)
        typer.echo(f'Subscribed to {node}.')

    return app


def radio_unsub(app: typer.Typer) -> typer.Typer:
    """Register the ``unsub`` command."""
    # node option
    node_help = 'Target node branch.'
    node = typer.Option(..., '--node', help=node_help)
    # channel option
    channel_help = 'Filter by channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'unsub')
    def _unsub(
        node: str = node,
        channel: Optional[str] = channel,
        path: str = path,
    ) -> None:
        """Unsubscribe from a node's channel."""
        radio = Radio(resolve_node(path))
        radio.unsubscribe(node, channel=channel)
        typer.echo(f'Unsubscribed from {node}.')

    return app


def radio_subs(app: typer.Typer) -> typer.Typer:
    """Register the ``subs`` command."""
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'subs')
    def _subs(
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List all subscriptions."""
        radio = Radio(resolve_node(path))
        rows = radio.subs()
        print_rows(rows, csv=csv, columns=_SUB_COLUMNS)

    return app


# ------ helper functions


def _resolve_reader() -> Node:
    """Resolve the acting reader: the calling node, else the cwd's node.

    ``_run.sh`` exports ``_NODE`` for the node whose loop is running, so a
    production read attributes to that node wherever it runs from; ``--path``
    never selects the reader -- it only picks the mailbox being viewed.

    Returns:
        Node the read receipts attribute to.

    Raises:
        typer.BadParameter: If no reader resolves (no ``_NODE`` and no
            node at the cwd).

    """
    if caller := Node._resolve_caller():
        return caller
    # the missing piece here is the reader, not the mailbox, so replace
    # resolve_node's generic advice (`fractal init` the cwd) with the remedy
    try:
        return resolve_node('.')
    except typer.BadParameter:
        raise typer.BadParameter(
            'No reader node: run from a node worktree or export _NODE'
            ' (--path only selects the mailbox viewed, never the reader).'
        ) from None


def _read_filter(all_messages: bool, read_messages: bool) -> Optional[bool]:
    """Resolve the read-state filter from the ``--all``/``--read`` flags.

    Args:
        all_messages: Whether ``--all`` was passed.
        read_messages: Whether ``--read`` was passed.

    Returns:
        ``None`` for everything, ``True`` for read only, ``False`` for unread
        only (the default).

    """
    # default: unread only; --all: everything; --read: read only
    if all_messages:
        return None
    elif read_messages:
        return True
    else:
        return False
