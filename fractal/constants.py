"""Constants for ``fractal``."""

FRACTAL_FOLDER = '.fractal'
WORKTREES_FOLDER = '.worktrees'
PROJECT_FOLDER = '.project'

CONFIG_FILE = 'config.json'
DB_FILE = '.db'
LOCK_FILE = '.lock'

# NOTE: the loop marker filenames below are git-ignored via the
#   _assets/git/exclude template -- retire or add markers in both
#   places in the same change (the exclude<->markers lockstep)
PAUSE_ABORT_FILE = '.pause_abort'
PAUSED_FILE = '.paused'
PGID_FILE = '.pgid'
SESSION_FILE = '.session'
SOCKET_FILE = '.socket'
STATUS_FILE = '.status'
STEP_PGID_FILE = '.step_pgid'

STATUSES = (
    'active',
    'paused',
    'idle',
    'completed',
    'stopped',
    'exited',
    'killed',
    'failed',
    'retired',
)
EVENTS = (
    'init',
    'spawn',
    'commit',
    'approve',
    'merge',
    'delete',
    'orphan',
    'start',
    'finish',
    'finish_cancel',
    'stop',
    'kill',
    'pause',
    'resume',
    'retire',
    'unretire',
)

PRIORITY_MIN = 0
PRIORITY_MAX = 10
