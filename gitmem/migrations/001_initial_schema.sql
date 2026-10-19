CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE units (
    id TEXT PRIMARY KEY,
    unit_type TEXT NOT NULL CHECK (unit_type IN (
        'turn', 'decision', 'code', 'constraint', 'fact', 'preference'
    )),
    topic TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    original_sha256 TEXT NOT NULL,
    shredded_at TEXT
);

CREATE TABLE unit_revisions (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    text TEXT,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edit_reason TEXT NOT NULL DEFAULT '',
    shredded_at TEXT,
    UNIQUE (unit_id, revision_number),
    CHECK (
        (text IS NOT NULL AND shredded_at IS NULL)
        OR (text IS NULL AND shredded_at IS NOT NULL)
    )
);

CREATE TABLE summaries (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    faithfulness_passed INTEGER NOT NULL CHECK (faithfulness_passed IN (0, 1)),
    verifier_report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE summary_sources (
    summary_id TEXT NOT NULL REFERENCES summaries(id),
    unit_id TEXT NOT NULL REFERENCES units(id),
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    PRIMARY KEY (summary_id, unit_id),
    UNIQUE (summary_id, source_order)
);

CREATE TABLE commits (
    hash TEXT PRIMARY KEY CHECK (length(hash) = 64),
    first_parent_hash TEXT REFERENCES commits(hash),
    second_parent_hash TEXT REFERENCES commits(hash),
    branch TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message TEXT NOT NULL,
    topic TEXT,
    tokens_before INTEGER NOT NULL CHECK (tokens_before >= 0),
    tokens_after INTEGER NOT NULL CHECK (tokens_after >= 0),
    snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64),
    payload_json TEXT NOT NULL DEFAULT '{}',
    CHECK (
        second_parent_hash IS NULL
        OR first_parent_hash IS NOT NULL
    )
);

CREATE TABLE commit_decisions (
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    unit_id TEXT NOT NULL REFERENCES units(id),
    action TEXT NOT NULL CHECK (action IN (
        'KEEP_VERBATIM', 'SUMMARIZE', 'ARCHIVE'
    )),
    reason TEXT NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    tier_before TEXT CHECK (tier_before IN ('pinned', 'hot', 'warm', 'cold')),
    tier_after TEXT NOT NULL CHECK (tier_after IN ('pinned', 'hot', 'warm', 'cold')),
    tokens_before INTEGER NOT NULL CHECK (tokens_before >= 0),
    tokens_after INTEGER NOT NULL CHECK (tokens_after >= 0),
    summary_id TEXT REFERENCES summaries(id),
    PRIMARY KEY (commit_hash, unit_id)
);

CREATE TABLE commit_membership (
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    item_id TEXT NOT NULL,
    item_kind TEXT NOT NULL CHECK (item_kind IN ('unit', 'summary')),
    unit_revision_id TEXT REFERENCES unit_revisions(id),
    summary_id TEXT REFERENCES summaries(id),
    tier TEXT NOT NULL CHECK (tier IN ('pinned', 'hot', 'warm', 'cold')),
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    PRIMARY KEY (commit_hash, item_id),
    CHECK (
        (item_kind = 'unit' AND unit_revision_id IS NOT NULL AND summary_id IS NULL)
        OR (item_kind = 'summary' AND unit_revision_id IS NULL AND summary_id IS NOT NULL)
    )
);

CREATE TABLE branches (
    name TEXT PRIMARY KEY,
    head_hash TEXT REFERENCES commits(hash),
    created_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE tags (
    name TEXT PRIMARY KEY,
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    created_at TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE unit_relations (
    from_unit_id TEXT NOT NULL REFERENCES units(id),
    to_unit_id TEXT NOT NULL REFERENCES units(id),
    relation TEXT NOT NULL CHECK (relation IN (
        'supersedes', 'contradicts', 'derived_from', 'related_to'
    )),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_unit_id, to_unit_id, relation),
    CHECK (from_unit_id <> to_unit_id)
);

CREATE TABLE merge_conflicts (
    id TEXT PRIMARY KEY,
    merge_commit_hash TEXT REFERENCES commits(hash),
    base_hash TEXT REFERENCES commits(hash),
    ours_hash TEXT NOT NULL REFERENCES commits(hash),
    theirs_hash TEXT NOT NULL REFERENCES commits(hash),
    unit_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open', 'resolved_keep_ours', 'resolved_keep_theirs',
        'resolved_keep_both', 'dismissed'
    )),
    resolution_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE canary_probes (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(id),
    question TEXT NOT NULL,
    expected_answer TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE canary_runs (
    id TEXT PRIMARY KEY,
    probe_id TEXT NOT NULL REFERENCES canary_probes(id),
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    actual_answer TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    created_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (probe_id, commit_hash)
);

CREATE TABLE answer_runs (
    id TEXT PRIMARY KEY,
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    branch TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    model_name TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE answer_sources (
    answer_run_id TEXT NOT NULL REFERENCES answer_runs(id),
    unit_id TEXT NOT NULL REFERENCES units(id),
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    decision TEXT NOT NULL CHECK (decision IN (
        'KEEP_VERBATIM', 'SUMMARIZE', 'ARCHIVE'
    )),
    contribution TEXT NOT NULL,
    PRIMARY KEY (answer_run_id, unit_id)
);

CREATE TABLE shred_tombstones (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(id),
    commit_hash TEXT NOT NULL REFERENCES commits(hash),
    original_sha256 TEXT NOT NULL,
    shredded_revision_hashes_json TEXT NOT NULL,
    confirmation_text TEXT NOT NULL,
    shredded_at TEXT NOT NULL
);

CREATE TABLE benchmark_runs (
    id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    strategy TEXT NOT NULL,
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    estimated_cost REAL NOT NULL CHECK (estimated_cost >= 0),
    response_latency_ms REAL NOT NULL CHECK (response_latency_ms >= 0),
    factual_accuracy REAL NOT NULL CHECK (factual_accuracy BETWEEN 0 AND 1),
    critical_information_recall REAL NOT NULL CHECK (
        critical_information_recall BETWEEN 0 AND 1
    ),
    recovery_success INTEGER NOT NULL CHECK (recovery_success IN (0, 1)),
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_units_session_created ON units(session_id, created_at);
CREATE INDEX idx_units_topic ON units(topic);
CREATE INDEX idx_revisions_unit ON unit_revisions(unit_id, revision_number);
CREATE INDEX idx_commits_branch_created ON commits(branch, created_at);
CREATE INDEX idx_commit_decisions_unit ON commit_decisions(unit_id, commit_hash);
CREATE INDEX idx_membership_commit_tier ON commit_membership(commit_hash, tier);
CREATE INDEX idx_canary_runs_commit ON canary_runs(commit_hash);
CREATE INDEX idx_answer_runs_commit ON answer_runs(commit_hash);
CREATE INDEX idx_conflicts_status ON merge_conflicts(status, created_at);
