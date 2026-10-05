-- Lagrange: connections to other services with no new code per service (Phase 11). Expand-only.
--
-- A connection is one service: an MCP server (at a URL, or a pinned npm/PyPI package run inside Lagrange),
-- Home Assistant, or a webhook in or out. Its name is the function its tools are registered under
-- ("gitea.list_repos"). Its tools are pinned when he connects it: a tool the service adds or changes later is
-- held (pending) until he approves it.
CREATE TABLE IF NOT EXISTS connections (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL UNIQUE,                -- the function name: gitea, home_assistant
    label         text NOT NULL,                       -- as he wrote it: "Gitea"
    kind          text NOT NULL,                       -- mcp_http | mcp_local | home_assistant | hook_in | hook_out
    config        jsonb NOT NULL DEFAULT '{}',         -- url, package, args, which secrets; never a secret itself
    status        text NOT NULL DEFAULT 'draft',       -- draft | connected | off | error | needs_signin
    message       text,                                -- the last thing worth saying about it
    tools         jsonb NOT NULL DEFAULT '[]',         -- approved (pinned) tools
    pending       jsonb NOT NULL DEFAULT '[]',         -- new or changed tools, waiting for his OK
    created_at    timestamptz NOT NULL DEFAULT now(),
    connected_at  timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    last_fired_at timestamptz                          -- a webhook in: when it was last called
);

-- Secrets, encrypted with a key only Lagrange holds (data/lagrange/vault.key). The orchestrator can see these
-- rows but can't open them.
CREATE TABLE IF NOT EXISTS connection_secrets (
    connection_id bigint NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    slot          text NOT NULL,                       -- token | header | oauth | hook | env:NAME
    nonce         bytea NOT NULL,
    box           bytea NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, slot)
);

-- A sign-in in progress: the one-time state the service's redirect must bring back (10 minutes).
CREATE TABLE IF NOT EXISTS connection_signins (
    state         text PRIMARY KEY,
    connection_id bigint NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    nonce         bytea NOT NULL,                      -- the PKCE verifier, sealed like any secret
    box           bytea NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
