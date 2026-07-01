"""End-to-end tests for the ``fractal radio`` CLI across two nodes.

The radio layer is inter-node messaging over the tree's central database:
each node owns a channel-space (``public``/``private``/``inbox``/``outbox``
plus custom ones) whose rows carry the hosting ``node``, with messages,
subscriptions, reactions, and read receipts. These tests drive the real
``fractal`` console script as a subprocess against a throwaway repo with a
user (root) node and two worker nodes, exercising routing, permissions,
read tracking, threads, reactions, the archive, and channel management as
observable end-to-end workflows rather than internal state, including
machine-output guarantees (an empty query still emits a header).
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Optional

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_send_routes_across_nodes_by_channel',
    'test_send_rejects_write_only_and_out_of_range_priority',
    'test_node_and_parent_are_mutually_exclusive',
    'test_bare_send_lands_in_private',
    'test_bare_messages_defaults_to_inbox',
    'test_read_tracking_drives_messages_filters',
    'test_sent_lists_outbound_mail',
    'test_feed_fans_out_over_subscriptions',
    'test_reply_builds_thread_and_respects_write_only',
    'test_react_toggles_positive_and_negative',
    'test_save_unsave_round_trips_through_archive',
    'test_subscribe_unsubscribe_manage_subs',
    'test_channel_create_and_delete_lifecycle',
    'test_cross_node_read_emits_receipt_without_mutating_sender',
    'test_empty_messages_query_emits_a_header',
]


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repo with a user node and two worker nodes (alpha, beta).

    Built once through the real CLI so the tests exercise ``init`` (and the
    ``.git/info/exclude`` it writes), default-channel seeding, and the
    parent/child auto-subscriptions that ``Radio.init`` performs.

    Returns:
        Mapping of ``root``, ``alpha``, and ``beta`` worktree paths.

    """
    root = tmp_path_factory.mktemp('fractal_radio')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'radio@test.local')
    _git(root, 'config', 'user.name', 'radio')
    (root / 'README.md').write_text('# radio\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node, so node init then passes
    assert _run(root, 'init').returncode == 0
    for name in ('alpha', 'beta'):
        node = _run(root, 'node', 'init', name, '--agent', 'claude')
        assert node.returncode == 0
    return {
        'root': root,
        'alpha': root / '.worktrees' / 'main.alpha',
        'beta': root / '.worktrees' / 'main.beta',
    }


# ------ send routing and permissions


@pytest.mark.parametrize(
    ('channel', 'subject'),
    [
        ('public', 'pub'),
        ('inbox', 'inb'),
    ],
)
def test_send_routes_across_nodes_by_channel(
    repo: dict,
    channel: str,
    subject: str,
) -> None:
    """A targeted send lands in the recipient's channel-space, not the sender's.

    ``inbox`` (others may write) and ``public`` (open) both accept a
    cross-node write; the message appears in beta's own message list (and
    alpha's ``sent``, recipient-attributed) and carries alpha as the sender
    -- alpha's own mailbox never lists it.
    """
    alpha, beta = repo['alpha'], repo['beta']
    body = f'routed via {channel}'
    uuid = _send(
        alpha,
        body,
        channel=channel,
        subject=subject,
        node='main.beta',
    )
    # bare `messages` now defaults to inbox; name the channel the send targeted
    listing = _radio(beta, 'messages', '--all', '--channel', channel).stdout
    assert uuid in listing
    assert body in listing
    assert 'main.alpha' in listing
    # the sender's mailbox stays empty; its `sent` carries the recipient
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', channel).stdout
    sent = _radio(alpha, 'sent', '--channel', channel).stdout
    assert uuid in sent
    assert 'main.beta' in sent


@pytest.mark.parametrize('channel', ['private', 'outbox'])
def test_send_rejects_write_only_and_out_of_range_priority(
    repo: dict,
    channel: str,
) -> None:
    """Write-only channels and out-of-range priorities are refused cleanly.

    ``private`` and ``outbox`` are write-only (owner only), so a foreign
    send is a permission error; a priority outside 0-10 is a value error.
    Both are domain errors raised in the core, so they must surface through
    the ``@command`` wrapper as a clean ``Error: <message>`` (exit 1) -- never
    the raw ``PermissionError:``/``ValueError:`` class name that reads like an
    uncaught crash.
    """
    alpha = repo['alpha']
    # foreign write into a write-only channel is rejected
    blocked = _radio(
        alpha,
        'send',
        'nope',
        '--channel',
        channel,
        '--node',
        'main.beta',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert blocked.returncode == 1
    assert blocked.stderr.startswith('Error:')
    assert 'PermissionError' not in blocked.stderr
    assert 'write-only' in blocked.stderr.lower()
    # priority above the 0-10 range is rejected
    too_high = _radio(
        alpha,
        'send',
        'nope',
        '--channel',
        'inbox',
        '--subject',
        's',
        '--priority',
        '11',
    )
    assert too_high.returncode == 1
    assert too_high.stderr.startswith('Error:')
    assert 'ValueError' not in too_high.stderr
    assert 'priority' in too_high.stderr.lower()


def test_node_and_parent_are_mutually_exclusive(repo: dict) -> None:
    """``--node`` and ``--parent`` cannot be combined.

    ``--parent`` addresses the structural parent (``main`` for
    ``main.alpha``), so pairing it with an explicit ``--node`` is
    contradictory and rejected. A bare ``--parent`` send succeeds.
    """
    alpha = repo['alpha']
    clash = _radio(
        alpha,
        'send',
        'x',
        '--channel',
        'public',
        '--node',
        'main.beta',
        '--parent',
        '--subject',
        's',
        '--priority',
        '3',
    )
    assert clash.returncode != 0
    assert 'mutually exclusive' in clash.stderr.lower()
    # a parent-directed send resolves to the user node and succeeds
    parent_send = _radio(
        alpha,
        'send',
        'hi parent',
        '--channel',
        'public',
        '--parent',
        '--subject',
        'p',
        '--priority',
        '4',
    )
    assert parent_send.returncode == 0
    uuid = parent_send.stdout.strip()
    # bare `messages` now defaults to inbox; this send targeted `public`
    root_listing = _radio(
        repo['root'],
        'messages',
        '--all',
        '--channel',
        'public',
    ).stdout
    assert uuid in root_listing


def test_bare_send_lands_in_private(repo: dict) -> None:
    """A bare ``send`` (no --node/--parent/--channel) is a self-note in `private`.

    Regression: a no-target send used to default to the sender's own `inbox`,
    where a "report" silently vanished; it now lands in `private` instead.
    """
    alpha = repo['alpha']
    sent = _radio(
        alpha,
        'send',
        'a private note',
        '--subject',
        'note',
        '--priority',
        '5',
    )
    assert sent.returncode == 0, sent.stderr
    uuid = sent.stdout.strip()
    assert uuid in _radio(alpha, 'messages', '--all', '--channel', 'private').stdout
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', 'inbox').stdout


def test_bare_messages_defaults_to_inbox(repo: dict) -> None:
    """Bare ``messages`` shows only the inbox, silently when piped.

    Regression: a bare ``messages`` used to comingle the inbox with the caller's
    own outbox/private rows; it now defaults to inbox. The defaulting notice is
    TTY-only -- a piped caller (an agent reading radio every sync) gets silence.
    """
    alpha = repo['alpha']
    inbox_uuid = _send(alpha, 'to inbox', channel='inbox', subject='inb2')
    # bare send -> private (see test above)
    priv_uuid = _radio(
        alpha,
        'send',
        'to private',
        '--subject',
        'priv2',
        '--priority',
        '5',
    ).stdout.strip()
    bare = _radio(alpha, 'messages', '--all')
    assert inbox_uuid in bare.stdout  # inbox is shown
    assert priv_uuid not in bare.stdout  # private is not (bare = inbox only)
    assert 'defaulting to inbox' not in bare.stderr  # notice is TTY-only


# ------ read tracking, feed, threads


def test_read_tracking_drives_messages_filters(repo: dict) -> None:
    """``messages`` defaults to unread; reading flips a message to read.

    A fresh self-message shows under the default (unread) view and under
    ``--all``; once read it disappears from the default view but appears
    under ``--read``. ``read`` itself echoes the full message including
    its UUID.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'unread body', channel='inbox', subject='track')
    # unread by default and under --all
    assert uuid in _radio(alpha, 'messages').stdout
    assert uuid in _radio(alpha, 'messages', '--all').stdout
    # reading echoes the message and marks it read
    shown = _radio(alpha, 'read', uuid)
    assert shown.returncode == 0
    assert uuid in shown.stdout
    assert 'unread body' in shown.stdout
    # now hidden from the default (unread) view, visible under --read
    assert uuid not in _radio(alpha, 'messages').stdout
    assert uuid in _radio(alpha, 'messages', '--read').stdout


def test_sent_lists_outbound_mail(repo: dict) -> None:
    """``sent`` lists own-authored messages with the recipient in ``node``.

    Outbound mail is invisible in the sender's own mailbox (it lives in the
    recipient's channel-space), so ``sent`` is the review surface: it lists
    what this node wrote, attributes each row to its host, and narrows with
    ``--channel``. A node that sent nothing still emits a header.
    """
    alpha = repo['alpha']
    to_beta = _send(alpha, 'for beta', channel='inbox', subject='sb', node='main.beta')
    to_self = _send(alpha, 'own note', channel='private', subject='sp')
    listing = _radio(alpha, 'sent').stdout
    assert to_beta in listing
    assert to_self in listing
    assert 'main.beta' in listing
    # --channel narrows to the matching host channel
    narrowed = _radio(alpha, 'sent', '--channel', 'private').stdout
    assert to_self in narrowed
    assert to_beta not in narrowed
    # a node that sent nothing still emits a header (machine output)
    empty = _radio(repo['root'], 'sent', '--channel', 'private')
    assert empty.stdout.strip() != ''
    assert to_self not in empty.stdout


def test_feed_fans_out_over_subscriptions(repo: dict) -> None:
    """``feed`` pulls readable messages from subscribed nodes.

    After alpha subscribes to beta's ``public`` channel and beta posts
    there, the message surfaces in alpha's feed (filtered to that
    channel). The feed marks it read, so the same query returns it under
    ``--read`` but not on the default unread pass.
    """
    alpha, beta = repo['alpha'], repo['beta']
    assert (
        _radio(alpha, 'sub', '--node', 'main.beta', '--channel', 'public').returncode
        == 0
    )
    uuid = _send(beta, 'fan-out body', channel='public', subject='feed', priority=6)
    # first pass: unread, pulled in and marked read as a side effect
    first = _radio(alpha, 'feed', '--node', 'main.beta', '--channel', 'public')
    assert first.returncode == 0
    assert uuid in first.stdout
    # default unread pass no longer shows it; --read does
    assert (
        uuid
        not in _radio(
            alpha,
            'feed',
            '--node',
            'main.beta',
            '--channel',
            'public',
        ).stdout
    )
    assert uuid in _radio(alpha, 'feed', '--channel', 'public', '--read').stdout


def test_reply_builds_thread_and_respects_write_only(repo: dict) -> None:
    """Replies nest under the root and cannot pierce a write-only channel.

    A local reply inherits the parent's subject (``Re: ...``) and shows
    as an indented descendant in ``thread``. A foreign reply into another
    node's write-only ``outbox`` is rejected with a permission error.
    """
    alpha, beta = repo['alpha'], repo['beta']
    root_uuid = _send(alpha, 'thread root', channel='public', subject='tree')
    reply = _radio(alpha, 'reply', root_uuid, 'a child')
    assert reply.returncode == 0
    child_uuid = reply.stdout.strip()
    # thread defaults to the full tree (no --all needed): both show even after
    # the root has been read, child indented beneath the root
    _radio(alpha, 'read', root_uuid)
    tree = _radio(alpha, 'thread', root_uuid).stdout
    assert root_uuid in tree
    assert child_uuid in tree
    assert 'Re: tree' in tree
    # a reply into beta's write-only outbox is refused
    out_uuid = _send(beta, 'owner only', channel='outbox', subject='ob')
    blocked = _radio(alpha, 'reply', out_uuid, 'inject')
    assert blocked.returncode != 0
    assert 'write-only' in blocked.stderr.lower()


# ------ reactions, archive, subscriptions


def test_react_toggles_positive_and_negative(repo: dict) -> None:
    """``react`` records a single vote per node, swapping +/- in place.

    A ``+`` reaction shows one positive react; re-reacting ``-`` replaces
    it (one negative, zero positive) rather than accumulating. A value
    other than +/- is rejected.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'vote on me', channel='inbox', subject='react')

    def _counts() -> str:
        """Return the archive-free counts line for the message."""
        rows = _radio(alpha, 'messages', '--all').stdout.splitlines()
        return next(line for line in rows if uuid in line)

    assert _radio(alpha, 'react', uuid, '+').returncode == 0
    positive = _counts()
    assert _radio(alpha, 'react', uuid, '-').returncode == 0
    negative = _counts()
    # the line must change as the single vote flips sign
    assert positive != negative
    # an invalid reaction value is rejected
    invalid = _radio(alpha, 'react', uuid, 'x')
    assert invalid.returncode != 0


def test_save_unsave_round_trips_through_archive(repo: dict) -> None:
    """``save`` archives a message; ``unsave`` removes it.

    A saved message appears under ``messages --saved`` and disappears
    after ``unsave``. ``--saved`` is mutually exclusive with ``--read``,
    and unsaving an unknown UUID is an error.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'keep me', channel='inbox', subject='save')
    assert _radio(alpha, 'save', uuid).returncode == 0
    assert uuid in _radio(alpha, 'messages', '--saved').stdout
    assert _radio(alpha, 'unsave', uuid).returncode == 0
    assert uuid not in _radio(alpha, 'messages', '--saved').stdout
    # --saved cannot be combined with --read
    clash = _radio(alpha, 'messages', '--saved', '--read')
    assert clash.returncode != 0
    # unsaving a message that was never archived is an error
    missing = _radio(alpha, 'unsave', 'DEADBEEF')
    assert missing.returncode != 0


def test_subscribe_unsubscribe_manage_subs(repo: dict) -> None:
    """``sub``/``unsub`` add and remove rows that ``subs`` lists.

    Subscribing alpha to beta's ``public`` channel adds a row naming the
    target node and channel; unsubscribing removes it while leaving the
    auto-seeded parent subscriptions intact.
    """
    alpha = repo['alpha']
    assert (
        _radio(alpha, 'sub', '--node', 'main.beta', '--channel', 'public').returncode
        == 0
    )
    subscribed = _radio(alpha, 'subs').stdout
    assert 'main.beta' in subscribed
    assert 'public' in subscribed
    assert _radio(alpha, 'unsub', '--node', 'main.beta').returncode == 0
    after = _radio(alpha, 'subs').stdout
    assert 'main.beta' not in after
    # the parent subscriptions seeded at init survive
    assert 'main' in after


# ------ channel management


def test_channel_create_and_delete_lifecycle(repo: dict) -> None:
    """Custom channels can be created, used, and deleted; defaults cannot.

    A new ``team`` channel is listed and accepts a self-send; creating a
    reserved default name or deleting a default channel is refused. A
    channel holding messages is refused without ``--force`` (mirroring
    ``unsend``) and removed with it, after which it no longer appears.
    """
    beta = repo['beta']
    assert _radio(beta, 'channel', 'create', 'team').returncode == 0
    assert 'team' in _radio(beta, 'channel', 'list').stdout
    # the new channel accepts a self-send
    posted = _send(beta, 'team body', channel='team', subject='t', priority=3)
    assert posted
    # default names are reserved for create and delete alike
    assert _radio(beta, 'channel', 'create', 'public').returncode != 0
    assert _radio(beta, 'channel', 'delete', 'inbox').returncode != 0
    # a channel holding messages is refused, but --force removes it
    assert _radio(beta, 'channel', 'delete', 'team').returncode != 0
    assert _radio(beta, 'channel', 'delete', 'team', '--force').returncode == 0
    assert 'team' not in _radio(beta, 'channel', 'list').stdout
    # deleting an unknown channel is an error
    assert _radio(beta, 'channel', 'delete', 'ghost').returncode != 0


def test_cross_node_read_emits_receipt_without_mutating_sender(
    repo: dict,
) -> None:
    """Reading a foreign message records a receipt, not an owner mutation.

    Alpha posts to its own readable ``outbox``; beta reads it directly by
    UUID (globally unique) and sees the full body. The read receipt is the
    reader's, so the message stays unread in alpha's own default view.
    """
    alpha, beta = repo['alpha'], repo['beta']
    uuid = _send(alpha, 'broadcast body', channel='outbox', subject='cast')
    remote = _radio(beta, 'read', uuid)
    assert remote.returncode == 0
    assert uuid in remote.stdout
    assert 'broadcast body' in remote.stdout
    # alpha never read it itself, so it stays in alpha's unread view
    assert uuid in _radio(alpha, 'messages', '--channel', 'outbox').stdout


# ------ machine output


def test_empty_messages_query_emits_a_header(repo: dict) -> None:
    """An empty ``messages`` query should emit a header, not nothing.

    ``node list`` passes ``columns=`` so an empty result still prints a
    header row; radio commands do not, so an empty result is zero bytes
    and indistinguishable from a failure when piped.
    """
    result = _radio(repo['beta'], 'messages', '--channel', 'private')
    assert result.stdout.strip() != ''


# ------ helpers


def _radio(path: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``fractal radio`` against the node at ``path``.

    The worktree is selected with ``--path`` so the command does not depend on
    the process working directory, which lets a single test drive both nodes.
    """
    return _run(path, 'radio', *args, '--path', f'{path}')


def _send(
    path: pathlib.Path,
    data: str,
    *,
    channel: str = 'inbox',
    subject: str = 's',
    priority: int = 5,
    node: Optional[str] = None,
) -> str:
    """Send a message and return its 8-char UUID."""
    args = [
        'send',
        data,
        '--channel',
        channel,
        '--subject',
        subject,
        '--priority',
        str(priority),
    ]
    if node is not None:
        args += ['--node', node]
    result = _radio(path, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
