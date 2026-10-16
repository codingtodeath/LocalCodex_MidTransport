-- 初始逻辑表结构。见 01_DEVELOPMENT_SPEC.md 第 13 节。
-- 约束原则：
--   * 不在任何表中保存 prompt、模型正文、Cookie、Authorization 或 OAuth 凭证。
--   * usage 的空值用 NULL；零只表示明确报告的零。
--   * 计量幂等靠 UNIQUE 约束，而不是应用层“看起来没写过”。

CREATE TABLE IF NOT EXISTS workers (
    worker_id            TEXT PRIMARY KEY,
    display_name         TEXT,
    current_instance_id  TEXT,
    last_seen_at         TEXT,
    last_seq             INTEGER NOT NULL DEFAULT 0,
    capabilities_json    TEXT,
    local_json           TEXT,
    account_epoch        TEXT,
    probe_ok             INTEGER,
    probe_at             TEXT,
    probe_instance_id    TEXT,
    state                TEXT NOT NULL DEFAULT 'unknown',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- 每个 (worker, instance) 的序号水位，用于心跳去重与旧实例隔离。
-- instance_seq 是“实例被首次见到的顺序”，用于判断谁才是最新实例：
-- 迟到的旧实例包不得覆盖新实例。
CREATE TABLE IF NOT EXISTS worker_instances (
    worker_id     TEXT NOT NULL,
    instance_id   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_seq      INTEGER NOT NULL DEFAULT 0,
    instance_seq  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (worker_id, instance_id)
);

CREATE TABLE IF NOT EXISTS client_keys (
    key_id          TEXT PRIMARY KEY,
    alias           TEXT NOT NULL,
    digest          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT,
    last_used_at    TEXT,
    rate_limit_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_keys_digest ON client_keys(digest);

CREATE TABLE IF NOT EXISTS requests (
    request_id            TEXT PRIMARY KEY,
    worker_id             TEXT,
    client_key_id         TEXT,
    client_alias          TEXT,
    requested_model       TEXT,
    response_model        TEXT,
    stream                INTEGER NOT NULL DEFAULT 0,
    started_at            TEXT NOT NULL,
    ended_at              TEXT,
    duration_ms           INTEGER,
    first_byte_ms         INTEGER,
    http_status           INTEGER,
    response_status       TEXT,
    error_code            TEXT,
    error_class           TEXT,
    upstream_response_id  TEXT,
    input_tokens          INTEGER,
    cached_input_tokens   INTEGER,
    cache_write_tokens    INTEGER,
    output_tokens         INTEGER,
    reasoning_tokens      INTEGER,
    total_tokens          INTEGER,
    usage_status          TEXT NOT NULL DEFAULT 'missing',
    usage_source          TEXT,
    price_version         TEXT,
    estimated_cost        TEXT,
    estimate_status       TEXT NOT NULL DEFAULT 'unavailable',
    currency              TEXT,
    usage_conflict        INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_started ON requests(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_requests_client ON requests(client_key_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(response_model, started_at DESC);

-- 一次 request 的一次出站尝试。第一版每个 request 只做一次显式尝试。
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id           TEXT PRIMARY KEY,
    request_id           TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
    attempt_no           INTEGER NOT NULL DEFAULT 1,
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    upstream_response_id TEXT,
    terminal_state       TEXT,
    terminal_seen        INTEGER NOT NULL DEFAULT 0,
    usage_recorded       INTEGER NOT NULL DEFAULT 0,
    http_status          INTEGER,
    bytes_relayed        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (request_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_attempts_request ON attempts(request_id);

-- 最近一次成功的额度快照。每个 worker + account_epoch 只有一条“当前”。
CREATE TABLE IF NOT EXISTS quota_snapshots (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id         TEXT NOT NULL,
    account_epoch     TEXT,
    account_alias     TEXT,
    identity_check    TEXT,
    plan_type         TEXT,
    status            TEXT NOT NULL,
    source            TEXT NOT NULL,
    captured_at       TEXT,
    received_at       TEXT NOT NULL,
    windows_json      TEXT NOT NULL DEFAULT '[]',
    credits_json      TEXT,
    error_code        TEXT,
    is_current        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_quota_current
    ON quota_snapshots(worker_id, is_current, captured_at DESC);

-- 每一次额度查询尝试的结果（成功与失败分开保存）。
CREATE TABLE IF NOT EXISTS quota_attempts (
    attempt_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id       TEXT NOT NULL,
    at              TEXT NOT NULL,
    result_category TEXT NOT NULL,
    error_code      TEXT,
    account_epoch   TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_attempts ON quota_attempts(worker_id, at DESC);

CREATE TABLE IF NOT EXISTS commands (
    command_id    TEXT PRIMARY KEY,
    worker_id     TEXT NOT NULL,
    command_type  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    delivered_at  TEXT,
    status        TEXT NOT NULL,
    error_code    TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_pending
    ON commands(worker_id, status, expires_at);

-- 不可变的价格规则版本。
CREATE TABLE IF NOT EXISTS price_rules (
    rule_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    price_version      TEXT NOT NULL,
    model              TEXT NOT NULL,
    service_tier       TEXT,
    modality           TEXT NOT NULL DEFAULT 'text',
    input_context_min  INTEGER NOT NULL DEFAULT 0,
    input_context_max  INTEGER,
    formula            TEXT NOT NULL,
    cache_write_policy TEXT NOT NULL DEFAULT 'not_applicable',
    price_input        TEXT NOT NULL,
    price_cached_input TEXT,
    price_output       TEXT,
    price_cache_write  TEXT,
    currency           TEXT NOT NULL,
    excluded_costs_json TEXT NOT NULL DEFAULT '[]',
    source_url         TEXT,
    verified_at        TEXT,
    effective_from     TEXT NOT NULL,
    created_by         TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (price_version, model, service_tier, input_context_min)
);
CREATE INDEX IF NOT EXISTS idx_price_rules_lookup
    ON price_rules(model, effective_from);

-- Windows 本地配额查不到时，凭据状态与账号级汇总（可选，独立口径）。
CREATE TABLE IF NOT EXISTS account_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    daily_json   TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_hash  TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    csrf_secret   TEXT NOT NULL,
    revoked_at    TEXT,
    user_agent    TEXT
);

-- 仅记录登录/Key/配置/服务状态事件，完全脱敏。
CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    category   TEXT NOT NULL,
    action     TEXT NOT NULL,
    actor      TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_events(at DESC);

-- 按日汇总，保留期长于逐请求元数据。
CREATE TABLE IF NOT EXISTS daily_rollups (
    day                TEXT NOT NULL,
    client_key_id      TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    request_count      INTEGER NOT NULL DEFAULT 0,
    success_count      INTEGER NOT NULL DEFAULT 0,
    failed_count       INTEGER NOT NULL DEFAULT 0,
    unknown_usage_count INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens       INTEGER NOT NULL DEFAULT 0,
    estimated_cost     TEXT NOT NULL DEFAULT '0',
    currency           TEXT NOT NULL DEFAULT 'USD',
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (day, client_key_id, model)
);

-- 项目服务密钥：每个 worker 的遥测 Key 摘要，以及 Gateway -> Bridge 的 wkr Key 摘要。
-- 只存摘要；明文只在创建时显示一次（或只存在于 Windows 与服务器秘密文件）。
CREATE TABLE IF NOT EXISTS worker_credentials (
    worker_id    TEXT NOT NULL,
    role         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (worker_id, role)
);

CREATE TABLE IF NOT EXISTS admin_credentials (
    admin_id     TEXT PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 管理员登录限速（按来源 IP 的短时计数）。
CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT NOT NULL,
    at         TEXT NOT NULL,
    success    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(ip, at DESC);
