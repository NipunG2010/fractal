"""Implements ``fractal radio`` sub-app commands."""

from __future__ import annotations

import sys
from typing import Optional

import typer

from fractal.cli.utils import command, print_rows, resolve_node
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

_MESSAGE_COLUMNS = [
    'node',
    'message_uuid',
    'channel',
    'sender',
    'priority',
    'subject',
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
        "Channel name (default: 'inbox' if target node specified, else 'private')."
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
        if channel is None:
            channel = 'inbox' if (node or parent) else 'private'
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
        """List one of your channels, inbox by default (use 'feed' for subscribed nodes)."""
        # resolve node
        radio = Radio(resolve_node(path))
        # --saved: show archived messages
        if saved_messages:
            if read_messages or all_messages:
                raise typer.BadParameter(
                    '--saved is mutually exclusive with --read/--all.'
                )
            rows = radio.saved(limit=limit, since=since, recent=recent)
            print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)
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
        print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)

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
        """Read messages from subscribed nodes (parent + direct children only)."""
        # resolve node
        radio = Radio(resolve_node(path))
        # --saved: show archived messages
        if saved_messages:
            if read_messages or all_messages:
                raise typer.BadParameter(
                    '--saved is mutually exclusive with --read/--all.'
                )
            rows = radio.saved(limit=limit, since=since, recent=recent)
            print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)
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
        print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)

    return app


def radio_read(app: typer.Typer) -> typer.Typer:
    """Register the ``read`` command."""
    # message_uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'read')
    def _read(
        message_uuid: str = message_uuid,
        path: str = path,
    ) -> None:
        """Read a message by UUID."""
        radio = Radio(resolve_node(path))
        message = radio.read(message_uuid)
        uuid = message['message_uuid']
        sender = message['sender']
        node = message['node']
        timestamp = message['created_at']
        channel = message['channel']
        subject = message['subject']
        priority = message['priority']
        data = message['data']
        typer.echo(f'Message UUID: {uuid}')
        typer.echo(f'From: {sender}')
        typer.echo(f'Node: {node}')
        typer.echo(f'Timestamp: {timestamp}')
        typer.echo(f'Channel: {channel}')
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
            print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)
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
        reply_uuid = radio.reply(message_uuid, data, priority=priority)
        typer.echo(reply_uuid)

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
        print_rows(rows, csv=csv, columns=['target', 'channel'])

    return app


# ------ helper functions


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
