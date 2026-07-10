"""End-to-end tests for the ``fractal radio`` CLI across two nodes.

The radio layer is inter-node messaging over the tree's central database:
each node owns a channel-space (``public``/``private``/``inbox``/``outbox``
plus custom ones) whose rows carry the hosting ``node``, with messages,
subscriptions, reactions, and read receipts. These tests drive the real
``fractal`` console script as a subprocess against a throwaway repo with a
user (root) node and two worker nodes, exercising routing, permissions,
read tracking, threads, reactions, the archive, and channel management as
observable end-to-end workflows rather than internal state, including
machine-output guarantees (an empty query emits the same header a
populated one would).
"""

from __future__ import annotations

import pathlib
import subprocess
from csv import DictReader
from io import StringIO
from typing import Optional

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_send_routes_across_nodes_by_channel',
    'test_send_rejects_write_only_and_out_of_range_priority',
    'test_node_and_parent_are_mutually_exclusive',
    'test_bare_send_lands_in_outbox',
    'test_send_echoes_resolved_channel',
    'test_bare_messages_defaults_to_inbox',
    'test_read_tracking_drives_messages_filters',
    'test_read_multiple_uuids_and_shape_errors',
    'test_read_path_selects_mailbox_never_reader',
    'test_read_reader_follows_node_env',
    'test_read_refuses_cross_tree_mailbox',
    'test_read_without_reader_names_the_remedy',
    'test_listings_are_passive_and_metadata_only',
    'test_sent_lists_outbound_mail',
    'test_feed_fans_out_over_subscriptions',
    'test_feed_listing_passive_and_read_feed_catches_up',
    'test_reply_builds_thread_and_respects_write_only',
    'test_inbox_reply_visible_to_counterparty',
    'test_reply_echoes_resolved_destination',
    'test_outbox_reply_routes_to_sender_inbox',
    'test_react_toggles_positive_and_negative',
    'test_save_unsave_round_trips_through_archive',
    'test_subscribe_unsubscribe_manage_subs',
    'test_channel_create_and_delete_lifecycle',
    'test_cross_node_read_emits_receipt_without_mutating_sender',
    'test_empty_messages_query_emits_a_header',
    'test_empty_and_populated_headers_match',
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
    -- alpha's own mailbox never lists it. The listing is metadata-only;
    the body arrives through ``read``.
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
    # bare `messages` defaults to inbox; name the channel the send targeted
    listing = _radio(beta, 'messages', '--all', '--channel', channel).stdout
    assert uuid in listing
    assert 'main.alpha' in listing
    # the body never rides the listing -- read is the body surface
    assert body not in listing
    assert body in _radio(beta, 'read', uuid).stdout
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
    # bare `messages` defaults to inbox; this send targeted `public`
    root_listing = _radio(
        repo['root'],
        'messages',
        '--all',
        '--channel',
        'public',
    ).stdout
    assert uuid in root_listing


def test_bare_send_lands_in_outbox(repo: dict) -> None:
    """A bare ``send`` (no --node/--parent/--channel) reports to the `outbox`.

    A private default would make every doc-following status report vanish
    from the parent's feed; reporting out is the common case, so a bare
    send lands in the sender's own `outbox`.
    """
    alpha = repo['alpha']
    sent = _radio(
        alpha,
        'send',
        'a status report',
        '--subject',
        'report',
        '--priority',
        '5',
    )
    assert sent.returncode == 0, sent.stderr
    uuid = sent.stdout.strip()
    assert uuid in _radio(alpha, 'messages', '--all', '--channel', 'outbox').stdout
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', 'private').stdout
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', 'inbox').stdout
    # private stays reachable as an explicit opt-in
    note_uuid = _send(alpha, 'a private note', channel='private', subject='note')
    assert (
        note_uuid in _radio(alpha, 'messages', '--all', '--channel', 'private').stdout
    )


@pytest.mark.parametrize(
    ('target_args', 'channel', 'target'),
    [
        ([], 'outbox', 'main.alpha'),
        (['--node', 'main.beta'], 'inbox', 'main.beta'),
        (['--parent'], 'inbox', 'main'),
    ],
    ids=['bare', 'node', 'parent'],
)
def test_send_echoes_resolved_channel(
    repo: dict,
    target_args: list[str],
    channel: str,
    target: str,
) -> None:
    """Every ``send`` echoes its resolved channel and target to stderr.

    Misdelivery is visible immediately, for agents too (unconditional,
    not TTY-gated). Stdout stays exactly the message UUID so scripts
    capturing it keep working.
    """
    alpha = repo['alpha']
    sent = _radio(
        alpha,
        'send',
        'echo check',
        *target_args,
        '--subject',
        'echo',
        '--priority',
        '5',
    )
    assert sent.returncode == 0, sent.stderr
    assert f"sent to {target}'s '{channel}' channel" in sent.stderr
    # stdout is the bare UUID, nothing else
    assert sent.stdout.strip() == sent.stdout.strip().splitlines()[0]
    assert len(sent.stdout.strip()) == 8


def test_bare_messages_defaults_to_inbox(repo: dict) -> None:
    """Bare ``messages`` shows only the inbox, silently when piped.

    Comingling the inbox with the caller's own outbox/private rows would
    bury inbound mail, so a bare ``messages`` defaults to inbox. The
    defaulting notice is TTY-only -- a piped caller (an agent reading
    radio every sync) gets silence.
    """
    alpha = repo['alpha']
    inbox_uuid = _send(alpha, 'to inbox', channel='inbox', subject='inb2')
    # bare send -> own outbox (see test above)
    outbox_uuid = _radio(
        alpha,
        'send',
        'to outbox',
        '--subject',
        'out2',
        '--priority',
        '5',
    ).stdout.strip()
    bare = _radio(alpha, 'messages', '--all')
    assert inbox_uuid in bare.stdout  # inbox is shown
    assert outbox_uuid not in bare.stdout  # outbox is not (bare = inbox only)
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


def test_read_multiple_uuids_and_shape_errors(repo: dict) -> None:
    """``read`` prints every named UUID once; malformed shapes are rejected."""
    alpha = repo['alpha']
    first = _send(alpha, 'first body', channel='inbox', subject='m1')
    second = _send(alpha, 'second body', channel='inbox', subject='m2')
    shown = _radio(alpha, 'read', first, second, first)
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.count('first body') == 1
    assert shown.stdout.count('second body') == 1
    assert shown.stdout.index('first body') < shown.stdout.index('second body')
    # a bare read has nothing to read
    bare = _radio(alpha, 'read')
    assert bare.returncode != 0
    # --unread needs a selector to narrow
    narrowed = _radio(alpha, 'read', first, '--unread')
    assert narrowed.returncode != 0


def test_read_path_selects_mailbox_never_reader(repo: dict) -> None:
    """``--path`` picks the mailbox viewed; receipts name the actual reader.

    The reader is the cwd-resolved node (``_NODE`` in production loops),
    never ``--path`` -- so a peek receipts as the peeker, and a read-only
    channel can never be impersonated via ``--path``.
    """
    alpha, root = repo['alpha'], repo['root']
    uuid = _send(alpha, 'peek body', channel='outbox', subject='peek')
    # the operator peeks at alpha's outbox from the root worktree
    peek = _run(root, 'radio', 'read', '--channel', 'outbox', '--path', f'{alpha}')
    assert peek.returncode == 0, peek.stderr
    assert 'peek body' in peek.stdout
    # the receipt is the root's: alpha's own unread view never moved ...
    assert uuid in _radio(alpha, 'messages', '--channel', 'outbox').stdout
    # ... while the root's next unread-narrowed peek skips the row
    again = _run(
        root,
        'radio',
        'read',
        '--channel',
        'outbox',
        '--unread',
        '--path',
        f'{alpha}',
    )
    assert again.returncode == 0, again.stderr
    assert uuid not in again.stdout
    # a read-only channel never impersonates: the root cannot read alpha's inbox
    _send(alpha, 'owner only', channel='inbox', subject='own')
    denied = _run(root, 'radio', 'read', '--channel', 'inbox', '--path', f'{alpha}')
    assert denied.returncode == 1
    assert 'read-only' in denied.stderr


def test_read_reader_follows_node_env(repo: dict) -> None:
    """An exported ``_NODE`` names the reader regardless of cwd and ``--path``.

    Production loops export ``_NODE`` for the node they drive, so a read run
    from anywhere attributes its receipts to that node; without it the cwd's
    node reads.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    uuid = _send(beta, 'env body', channel='public', subject='env')
    # read from the ROOT worktree with alpha's identity exported
    shown = _run(root, 'radio', 'read', uuid, _NODE=f'{alpha}')
    assert shown.returncode == 0, shown.stderr
    assert 'env body' in shown.stdout
    # the receipt is alpha's: alpha's unread-narrowed view skips the row ...
    as_alpha = _run(
        root,
        'radio',
        'read',
        '--channel',
        'public',
        '--unread',
        '--path',
        f'{beta}',
        _NODE=f'{alpha}',
    )
    assert as_alpha.returncode == 0, as_alpha.stderr
    assert uuid not in as_alpha.stdout
    # ... while the root (no _NODE) never read it and still gets the body
    as_root = _run(
        root,
        'radio',
        'read',
        '--channel',
        'public',
        '--unread',
        '--path',
        f'{beta}',
    )
    assert as_root.returncode == 0, as_root.stderr
    assert uuid in as_root.stdout


def test_read_refuses_cross_tree_mailbox(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """A ``--path`` into another fractal tree is refused, never silently mixed.

    The reader's radio resolves branch names against its own central DB, and
    branch names collide across trees by construction (every tree roots at
    ``main``), so a foreign mailbox would silently read -- and receipt -- the
    reader's own same-named mailbox.
    """
    root = repo['root']
    # a second tree whose root branch collides with the reader's ('main')
    other = tmp_path / 'other'
    other.mkdir()
    _git(other, 'init', '-b', 'main')
    _git(other, 'config', 'user.email', 'radio@test.local')
    _git(other, 'config', 'user.name', 'radio')
    (other / 'README.md').write_text('# other\n', encoding='utf-8')
    wiki = other / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(other, 'add', '-A')
    _git(other, 'commit', '-m', 'init')
    assert _run(other, 'init').returncode == 0
    # each root posts to its own outbox; only --path distinguishes them
    _send(root, 'home body', channel='outbox', subject='home')
    _send(other, 'away body', channel='outbox', subject='away')
    refused = _run(root, 'radio', 'read', '--channel', 'outbox', '--path', f'{other}')
    assert refused.returncode != 0
    assert 'different fractal tree' in refused.stderr
    assert 'home body' not in refused.stdout
    assert 'away body' not in refused.stdout


def test_read_without_reader_names_the_remedy(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """A read with no resolvable reader says how to become one.

    From a cwd outside any node (no ``_NODE`` exported), the missing piece
    is the reader identity, not the mailbox -- ``--path`` already names a
    real node. The error must name the actual remedy, not ``resolve_node``'s
    generic advice to ``fractal init`` the operator's cwd.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'reader body', channel='outbox', subject='who')
    lost = _run(tmp_path, 'radio', 'read', uuid, '--path', f'{alpha}')
    assert lost.returncode != 0
    assert 'No reader node' in lost.stderr
    assert 'fractal init' not in lost.stderr


def test_listings_are_passive_and_metadata_only(repo: dict) -> None:
    """``messages`` never writes receipts and never prints bodies.

    Every ``_radio`` call is a fresh ``fractal`` process, so each assertion
    crosses a session/run boundary; receipts move only when the reader
    reads, and only for the rows the read selected.
    """
    beta = repo['beta']
    marked = _send(beta, 'triaged body', channel='inbox', subject='mk1')
    spared = _send(beta, 'later body', channel='private', subject='mk2')
    # plain listings are passive: the row stays unread across two runs
    assert marked in _radio(beta, 'messages').stdout
    listing = _radio(beta, 'messages')
    assert marked in listing.stdout
    # ... and metadata-only: the subject rides, the body and its column don't
    assert 'mk1' in listing.stdout
    assert 'triaged body' not in listing.stdout
    header = listing.stdout.splitlines()[0]
    assert 'data' not in header.split(',')
    # listings are passive: no --mark-read flag exists to force a receipt
    refused = _radio(beta, 'messages', '--mark-read')
    assert refused.returncode != 0
    # reading is the consuming act; the receipt persists across runs
    shown = _radio(beta, 'read', '--channel', 'inbox', '--unread')
    assert shown.returncode == 0, shown.stderr
    assert 'triaged body' in shown.stdout
    assert marked not in _radio(beta, 'messages').stdout
    assert marked in _radio(beta, 'messages', '--read').stdout
    # rows outside the read's selection were untouched
    assert spared in _radio(beta, 'messages', '--channel', 'private').stdout


def test_sent_lists_outbound_mail(repo: dict) -> None:
    """``sent`` lists own-authored messages with the recipient in ``node``.

    Outbound mail is invisible in the sender's own mailbox (it lives in the
    recipient's channel-space), so ``sent`` is the review surface: it lists
    what this node wrote -- bodies included, unlike the metadata listings --
    attributes each row to its host, and narrows with ``--channel``. A node
    that sent nothing still emits a header.
    """
    alpha = repo['alpha']
    to_beta = _send(alpha, 'for beta', channel='inbox', subject='sb', node='main.beta')
    to_self = _send(alpha, 'own note', channel='private', subject='sp')
    listing = _radio(alpha, 'sent').stdout
    assert to_beta in listing
    assert to_self in listing
    assert 'main.beta' in listing
    # sent keeps the body column: it reviews what this node actually wrote
    assert 'own note' in listing
    # --channel narrows to the matching host channel
    narrowed = _radio(alpha, 'sent', '--channel', 'private').stdout
    assert to_self in narrowed
    assert to_beta not in narrowed
    # a node that sent nothing still emits a header (machine output)
    empty = _radio(repo['root'], 'sent', '--channel', 'private')
    assert empty.stdout.strip() != ''
    assert to_self not in empty.stdout


def test_feed_fans_out_over_subscriptions(repo: dict) -> None:
    """``feed`` pulls readable messages from subscribed nodes, passively.

    After alpha subscribes to beta's ``public`` channel and beta posts
    there, the message surfaces in alpha's feed (filtered to that
    channel). Listing is passive (mirroring ``messages``): the same unread
    row re-lists on the next pass, and no receipt shows under ``--read``.
    """
    alpha, beta = repo['alpha'], repo['beta']
    assert (
        _radio(alpha, 'sub', '--node', 'main.beta', '--channel', 'public').returncode
        == 0
    )
    uuid = _send(beta, 'fan-out body', channel='public', subject='feed', priority=6)
    # the subscribed row surfaces on the default (unread) pass
    first = _radio(alpha, 'feed', '--node', 'main.beta', '--channel', 'public')
    assert first.returncode == 0
    assert uuid in first.stdout
    # listing is passive: the row re-lists unread, and no receipt exists
    assert (
        uuid
        in _radio(
            alpha,
            'feed',
            '--node',
            'main.beta',
            '--channel',
            'public',
        ).stdout
    )
    assert uuid not in _radio(alpha, 'feed', '--channel', 'public', '--read').stdout


def test_feed_listing_passive_and_read_feed_catches_up(repo: dict) -> None:
    """``feed`` lists metadata passively; ``read --feed --unread`` consumes.

    Mirror of the ``messages`` contract on the fan-out surface: every
    ``_radio`` call is a fresh ``fractal`` process, so each assertion
    crosses a session/run boundary.
    """
    alpha, beta = repo['alpha'], repo['beta']
    for channel in ('public', 'outbox'):
        sub = _radio(alpha, 'sub', '--node', 'main.beta', '--channel', channel)
        assert sub.returncode == 0
    post = _send(beta, 'post body', channel='public', subject='fm1')
    cast = _send(beta, 'cast body', channel='outbox', subject='fm2')
    # plain feeds are passive and metadata-only across runs
    first = _radio(alpha, 'feed', '--node', 'main.beta')
    assert post in first.stdout
    assert 'post body' not in first.stdout
    second = _radio(alpha, 'feed', '--node', 'main.beta')
    assert post in second.stdout
    # listings are passive: no --mark-read flag exists to force a receipt
    refused = _radio(alpha, 'feed', '--mark-read')
    assert refused.returncode != 0
    # the feed catch-up prints bodies and receipts them for this reader
    shown = _radio(alpha, 'read', '--feed', '--unread')
    assert shown.returncode == 0, shown.stderr
    assert 'post body' in shown.stdout
    assert 'cast body' in shown.stdout
    # the receipts persist into later runs: gone from the default (unread)
    # view, visible under --read, and the source's own state never moved
    assert post not in _radio(alpha, 'feed', '--node', 'main.beta').stdout
    assert cast in _radio(alpha, 'feed', '--channel', 'outbox', '--read').stdout
    assert cast in _radio(beta, 'messages', '--channel', 'outbox').stdout


def test_reply_builds_thread_and_respects_write_only(repo: dict) -> None:
    """Replies nest under the root and cannot pierce a write-only channel.

    A local reply inherits the parent's subject (``Re: ...``) and shows
    as an indented descendant in ``thread``. A foreign reply into another
    node's write-only ``outbox`` never lands in it -- it reroutes to the
    owner's inbox as a conversation turn.
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
    # a reply into beta's write-only outbox cannot pierce it -- the reply
    # reroutes to beta's inbox instead of being refused (the rerouted row
    # is asserted end-to-end in test_outbox_reply_routes_to_sender_inbox)
    out_uuid = _send(beta, 'owner only', channel='outbox', subject='ob')
    rerouted = _radio(alpha, 'reply', out_uuid, 'inject')
    assert rerouted.returncode == 0, rerouted.stderr
    assert "sent to main.beta's 'inbox' channel" in rerouted.stderr


def test_inbox_reply_visible_to_counterparty(repo: dict) -> None:
    """A counterparty-routed reply is visible to both parties end-to-end.

    A reply routed correctly at the row level can still be invisible if
    the message query keeps thread-roots only: the recipient's ``messages``
    (default and ``--all``) would miss it, the author's ``sent`` would miss
    it, and ``thread`` would error owner-only for the root's sender. All
    four symptoms share that one lesion, so one workflow asserts all four.
    """
    alpha, beta = repo['alpha'], repo['beta']
    # alpha mails beta's inbox; beta replies, which routes to alpha's inbox
    root_uuid = _send(alpha, 'question for beta', node='main.beta', subject='q')
    reply = _radio(beta, 'reply', root_uuid, 'the answer')
    assert reply.returncode == 0, reply.stderr
    reply_uuid = reply.stdout.strip().splitlines()[0]
    # the recipient's inbox view shows the routed reply
    inbox = _radio(alpha, 'messages', '--all')
    assert reply_uuid in inbox.stdout, inbox.stdout
    # the author's sent includes the counterparty-routed reply
    sent = _radio(beta, 'sent')
    assert reply_uuid in sent.stdout, sent.stdout
    # the thread reads whole for BOTH parties (the fourth symptom: an
    # owner-only error for the root's sender)
    for party in (alpha, beta):
        tree = _radio(party, 'thread', root_uuid)
        assert tree.returncode == 0, tree.stderr
        assert reply_uuid in tree.stdout, tree.stdout


def test_reply_echoes_resolved_destination(repo: dict) -> None:
    """``reply`` echoes its resolved destination to stderr like ``send``.

    A counterparty-routed reply lands in another node's channel-space, but
    a bare-UUID echo alone makes misdelivery invisible exactly where routing
    is least obvious. The echo mirrors ``send``'s: stderr, with stdout still
    exactly the UUID for capturing scripts.
    """
    alpha, beta = repo['alpha'], repo['beta']
    root_uuid = _send(alpha, 'ping', node='main.beta', subject='dest echo')
    reply = _radio(beta, 'reply', root_uuid, 'pong')
    assert reply.returncode == 0, reply.stderr
    assert "sent to main.alpha's 'inbox' channel" in reply.stderr


def test_outbox_reply_routes_to_sender_inbox(repo: dict) -> None:
    """A reply to another node's outbox message routes to the SENDER's inbox.

    The natural reaction to a feed post is ``reply`` on it, but a foreign
    outbox is write-only, and a bare refusal would force the whole fleet
    to work around it with fresh sends (channel context lost) -- so the
    reply routes to the sender's inbox, mirroring the inbox counterparty case.
    """
    alpha, beta = repo['alpha'], repo['beta']
    out_uuid = _send(beta, 'progress note', channel='outbox', subject='report')
    reply = _radio(alpha, 'reply', out_uuid, 'ack, steer starboard')
    assert reply.returncode == 0, reply.stderr
    reply_uuid = reply.stdout.strip().splitlines()[0]
    # the reply reached the sender's inbox, not the write-only outbox
    inbox = _radio(beta, 'messages', '--all')
    assert reply_uuid in inbox.stdout, inbox.stdout


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
    header row; radio commands must do the same -- a zero-byte empty
    result would be indistinguishable from a failure when piped.
    """
    result = _radio(repo['beta'], 'messages', '--channel', 'private')
    assert result.stdout.strip() != ''


def test_empty_and_populated_headers_match(repo: dict) -> None:
    """Empty and populated listings emit the identical header shape.

    Parsers key on the populated CSV shape, so an empty result must present
    the same columns. Each listing is captured populated and forced empty
    (``--since`` far in the future, or with every subscription removed); the
    header line must not change.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    far_future = '9999-01-01T00:00:00Z'
    # seed one row per listing: a targeted send (messages + sent), a root
    # outbox post pulled through beta's seeded parent subscription (feed),
    # and an archived copy (saved)
    uuid = _send(alpha, 'parity body', subject='par', node='main.beta')
    _send(root, 'parity feed body', channel='outbox', subject='parf')
    assert _radio(beta, 'save', uuid).returncode == 0
    pairs = [
        (
            _radio(beta, 'messages', '--all'),
            _radio(beta, 'messages', '--all', '--since', far_future),
        ),
        (
            _radio(alpha, 'sent'),
            _radio(alpha, 'sent', '--since', far_future),
        ),
        (
            _radio(beta, 'feed', '--all'),
            _radio(beta, 'feed', '--all', '--since', far_future),
        ),
        (
            _radio(beta, 'messages', '--saved'),
            _radio(beta, 'messages', '--saved', '--since', far_future),
        ),
        (
            _radio(beta, 'feed', '--saved'),
            _radio(beta, 'feed', '--saved', '--since', far_future),
        ),
    ]
    for populated, empty in pairs:
        assert populated.returncode == 0
        assert empty.returncode == 0
        head, *rows = populated.stdout.splitlines()
        assert rows, 'populated capture must hold at least one data row'
        assert empty.stdout.splitlines() == [head]
    # subs has no --since filter: empty it by removing the seeded parent
    # subscriptions, then restore them so later tests see the same state
    populated = _radio(root, 'subs')
    subs = list(DictReader(StringIO(populated.stdout)))
    assert subs, 'root holds the auto-seeded child subscriptions'
    for sub in subs:
        unsubbed = _radio(
            root,
            'unsub',
            '--node',
            sub['target'],
            '--channel',
            sub['channel'],
        )
        assert unsubbed.returncode == 0
    try:
        empty = _radio(root, 'subs')
        assert empty.stdout.splitlines() == [populated.stdout.splitlines()[0]]
    finally:
        for sub in subs:
            resubbed = _radio(
                root,
                'sub',
                '--node',
                sub['target'],
                '--channel',
                sub['channel'],
            )
            assert resubbed.returncode == 0


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
