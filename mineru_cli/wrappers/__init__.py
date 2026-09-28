"""Facade wrappers around live workspace engines under $MINERU_HOME/bin/.

Foundation increment (§0 of the 2026-07-25 capability spec) is facade-first:
the CLI never re-implements engine logic, it wraps the existing tools.

Each wrapper module here provides one narrow responsibility:

- Resolve the engine binary path (env override + documented default).
- Build the argv the engine expects.
- Shell out via `subprocess.run(check=False)` with stdio pass-through.
- Propagate the engine's exit code unchanged.

Wrappers deliberately do NOT parse, reformat, filter, or otherwise mutate
engine output. If a caller needs the raw JSON, they ask the engine for it
(via `--json`) and read the CLI's stdout themselves.

Firewall-preservation invariant (§0 + §3.1): Gmail/iMessage read verbs
MUST route through the firewalled wrapper, never the raw engine. The
`gog_firewall` module (Google Workspace reads) and `imsg_firewall`
module (iMessage reads) are the single argv[0] sources of truth for
their respective read paths; see their docstrings for the enforcement
story. The `imsg` module is the deliberately-separate outbound-only
wrapper for iMessage writes - the two files are kept apart so the
read/write boundary is grep-visible.
"""

from mineru_cli.wrappers.amazon_orders import (
    AMAZON_ORDERS_BIN_ENV,
    DEFAULT_AMAZON_ORDERS_BIN,
    build_amazon_orders_argv,
    resolve_amazon_orders_bin,
    run_amazon_orders,
)
from mineru_cli.wrappers.brevity import (
    BREVITY_BIN_ENV,
    DEFAULT_BREVITY_BIN,
    build_brevity_argv,
    resolve_brevity_bin,
    run_brevity,
)
from mineru_cli.wrappers.deliver_output import (
    DEFAULT_DELIVER_OUTPUT_BIN,
    DELIVER_OUTPUT_BIN_ENV,
    build_deliver_output_argv,
    resolve_deliver_output_bin,
    run_deliver_output,
)
from mineru_cli.wrappers.gog_firewall import (
    DEFAULT_GOG_FIREWALL_BIN,
    EXPECTED_BIN_BASENAME,
    EXPECTED_BIN_BASENAME as GOG_FIREWALL_EXPECTED_BASENAME,
    GOG_FIREWALL_BIN_ENV,
    build_gog_firewall_argv,
    resolve_gog_firewall_bin,
    run_gog_firewall,
)
from mineru_cli.wrappers.imsg import (
    DEFAULT_IMSG_BIN,
    EXPECTED_BIN_BASENAME as IMSG_EXPECTED_BASENAME,
    IMSG_BIN_ENV,
    build_imsg_argv,
    resolve_imsg_bin,
    run_imsg,
)
from mineru_cli.wrappers.imsg_firewall import (
    DEFAULT_IMSG_FIREWALL_BIN,
    EXPECTED_BIN_BASENAME as IMSG_FIREWALL_EXPECTED_BASENAME,
    IMSG_FIREWALL_BIN_ENV,
    build_imsg_firewall_argv,
    resolve_imsg_firewall_bin,
    run_imsg_firewall,
)
from mineru_cli.wrappers.monarch import (
    DEFAULT_MONARCH_BIN,
    MONARCH_BIN_ENV,
    build_monarch_argv,
    resolve_monarch_bin,
    run_monarch,
)
from mineru_cli.wrappers.msearch import (
    DEFAULT_MSEARCH_BIN,
    MSEARCH_BIN_ENV,
    resolve_msearch_bin,
    run_msearch,
)
from mineru_cli.wrappers.slack_channels import (
    DEFAULT_SLACK_CHANNELS_BIN,
    EXPECTED_BIN_BASENAME as SLACK_CHANNELS_EXPECTED_BASENAME,
    SLACK_CHANNELS_BIN_ENV,
    build_slack_channels_argv,
    resolve_slack_channels_bin,
    run_slack_channels,
)
from mineru_cli.wrappers.slack_read import (
    DEFAULT_SLACK_READ_BIN,
    EXPECTED_BIN_BASENAME as SLACK_READ_EXPECTED_BASENAME,
    SLACK_READ_BIN_ENV,
    build_slack_read_argv,
    resolve_slack_read_bin,
    run_slack_read,
)
from mineru_cli.wrappers.slack_refresh_users import (
    DEFAULT_SLACK_REFRESH_USERS_BIN,
    EXPECTED_BIN_BASENAME as SLACK_REFRESH_USERS_EXPECTED_BASENAME,
    SLACK_REFRESH_USERS_BIN_ENV,
    build_slack_refresh_users_argv,
    resolve_slack_refresh_users_bin,
    run_slack_refresh_users,
)
from mineru_cli.wrappers.slack_search_public import (
    DEFAULT_SLACK_SEARCH_PUBLIC_BIN,
    EXPECTED_BIN_BASENAME as SLACK_SEARCH_PUBLIC_EXPECTED_BASENAME,
    SLACK_SEARCH_PUBLIC_BIN_ENV,
    build_slack_search_public_argv,
    resolve_slack_search_public_bin,
    run_slack_search_public,
)
from mineru_cli.wrappers.slack_thread import (
    DEFAULT_SLACK_THREAD_BIN,
    EXPECTED_BIN_BASENAME as SLACK_THREAD_EXPECTED_BASENAME,
    SLACK_THREAD_BIN_ENV,
    build_slack_thread_argv,
    resolve_slack_thread_bin,
    run_slack_thread,
)
from mineru_cli.wrappers.telegram_image_cache import (
    CACHE_DIR_MODE,
    CACHE_FILE_MODE,
    DEFAULT_RETENTION_DAYS,
    MINERU_SENT_IMAGE_DIR_ENV,
    PendingCacheEntry,
    PruneReport,
    RETENTION_FOREVER,
    SentImageCacheError,
    SentImageRecord,
    cache_binary,
    commit_record,
    ensure_cache_dir,
    iter_records,
    lookup_by_sha256,
    prune_expired,
    resolve_cache_dir,
    search_records,
)
from mineru_cli.wrappers.telegram_photo import (
    TELEGRAM_API_HOST,
    TELEGRAM_BOT_TOKEN_SECRET,
    TELEGRAM_CAPTION_MAX_CHARS,
    TELEGRAM_CHAT_ID_SECRET,
    SendPhotoResult,
    send_photo,
)

__all__ = [
    "DEFAULT_GOG_FIREWALL_BIN",
    "DEFAULT_IMSG_BIN",
    "DEFAULT_IMSG_FIREWALL_BIN",
    "DEFAULT_MSEARCH_BIN",
    "DEFAULT_SLACK_CHANNELS_BIN",
    "DEFAULT_SLACK_READ_BIN",
    "DEFAULT_SLACK_REFRESH_USERS_BIN",
    "DEFAULT_SLACK_SEARCH_PUBLIC_BIN",
    "DEFAULT_SLACK_THREAD_BIN",
    "EXPECTED_BIN_BASENAME",
    "GOG_FIREWALL_EXPECTED_BASENAME",
    "IMSG_EXPECTED_BASENAME",
    "IMSG_FIREWALL_EXPECTED_BASENAME",
    "SLACK_CHANNELS_EXPECTED_BASENAME",
    "SLACK_READ_EXPECTED_BASENAME",
    "SLACK_REFRESH_USERS_EXPECTED_BASENAME",
    "SLACK_SEARCH_PUBLIC_EXPECTED_BASENAME",
    "SLACK_THREAD_EXPECTED_BASENAME",
    "GOG_FIREWALL_BIN_ENV",
    "IMSG_BIN_ENV",
    "IMSG_FIREWALL_BIN_ENV",
    "MSEARCH_BIN_ENV",
    "SLACK_CHANNELS_BIN_ENV",
    "SLACK_READ_BIN_ENV",
    "SLACK_REFRESH_USERS_BIN_ENV",
    "SLACK_SEARCH_PUBLIC_BIN_ENV",
    "SLACK_THREAD_BIN_ENV",
    "build_gog_firewall_argv",
    "build_imsg_argv",
    "build_imsg_firewall_argv",
    "build_slack_channels_argv",
    "build_slack_read_argv",
    "build_slack_refresh_users_argv",
    "build_slack_search_public_argv",
    "build_slack_thread_argv",
    "resolve_gog_firewall_bin",
    "resolve_imsg_bin",
    "resolve_imsg_firewall_bin",
    "resolve_msearch_bin",
    "resolve_slack_channels_bin",
    "resolve_slack_read_bin",
    "resolve_slack_refresh_users_bin",
    "resolve_slack_search_public_bin",
    "resolve_slack_thread_bin",
    "run_gog_firewall",
    "run_imsg",
    "run_imsg_firewall",
    "run_msearch",
    "run_slack_channels",
    "run_slack_read",
    "run_slack_refresh_users",
    "run_slack_search_public",
    "run_slack_thread",
    # Phase 2 connector / utility wrappers
    "AMAZON_ORDERS_BIN_ENV",
    "BREVITY_BIN_ENV",
    "DEFAULT_AMAZON_ORDERS_BIN",
    "DEFAULT_BREVITY_BIN",
    "DEFAULT_DELIVER_OUTPUT_BIN",
    "DEFAULT_MONARCH_BIN",
    "DELIVER_OUTPUT_BIN_ENV",
    "MONARCH_BIN_ENV",
    "build_amazon_orders_argv",
    "build_brevity_argv",
    "build_deliver_output_argv",
    "build_monarch_argv",
    "resolve_amazon_orders_bin",
    "resolve_brevity_bin",
    "resolve_deliver_output_bin",
    "resolve_monarch_bin",
    "run_amazon_orders",
    "run_brevity",
    "run_deliver_output",
    "run_monarch",
    # Phase 3: self-contained Telegram sendPhoto transport (P3-01)
    "SendPhotoResult",
    "TELEGRAM_API_HOST",
    "TELEGRAM_BOT_TOKEN_SECRET",
    "TELEGRAM_CAPTION_MAX_CHARS",
    "TELEGRAM_CHAT_ID_SECRET",
    "send_photo",
    # Phase 3: sent-image retention cache (P3-02)
    "CACHE_DIR_MODE",
    "CACHE_FILE_MODE",
    "DEFAULT_RETENTION_DAYS",
    "MINERU_SENT_IMAGE_DIR_ENV",
    "PendingCacheEntry",
    "PruneReport",
    "RETENTION_FOREVER",
    "SentImageCacheError",
    "SentImageRecord",
    "cache_binary",
    "commit_record",
    "ensure_cache_dir",
    "iter_records",
    "lookup_by_sha256",
    "prune_expired",
    "resolve_cache_dir",
    "search_records",
]
