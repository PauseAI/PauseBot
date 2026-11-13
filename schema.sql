-- Applied on every bot startup, so every statement must be idempotent.

-- Daily copy of each server's structure (roles, channels, member roles/nicks, bans), kept 30 days.
CREATE TABLE IF NOT EXISTS server_snapshots (
    id bigserial PRIMARY KEY,
    guild_id bigint NOT NULL,
    taken_at timestamptz NOT NULL DEFAULT now(),
    data jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS server_snapshots_guild_taken_at ON server_snapshots (guild_id, taken_at DESC);

-- Every message the bot sees, kept 30 days from when it was sent.
CREATE TABLE IF NOT EXISTS message_log (
    message_id bigint PRIMARY KEY,
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    author_id bigint NOT NULL,
    content text NOT NULL,
    attachments jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL,
    edited_at timestamptz,
    deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS message_log_created_at ON message_log (created_at);
CREATE INDEX IF NOT EXISTS message_log_channel_created_at ON message_log (channel_id, created_at);
CREATE INDEX IF NOT EXISTS message_log_author_created_at ON message_log (author_id, created_at);

-- Content a message had before each edit; message_log holds the current content.
CREATE TABLE IF NOT EXISTS message_edits (
    id bigserial PRIMARY KEY,
    message_id bigint NOT NULL REFERENCES message_log (message_id) ON DELETE CASCADE,
    content_before text NOT NULL,
    edited_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS message_edits_message_id ON message_edits (message_id);

-- One row per Discord user the bot has seen (moved from Airtable's Discord table). Kept after they
-- leave, so their roles can be given back if they rejoin.
CREATE TABLE IF NOT EXISTS discord_members (
    id bigint PRIMARY KEY,
    username text NOT NULL,
    nick text,
    global_name text,
    joined_at timestamptz,
    left_at timestamptz,
    role_ids bigint[] NOT NULL DEFAULT '{}',
    role_names text[] NOT NULL DEFAULT '{}',
    onboarding_notification boolean NOT NULL DEFAULT false,
    discord_reaction boolean NOT NULL DEFAULT false,
    -- When CiviCRM linked this Discord user to a member; the daily sweep retries everyone without one
    civicrm_linked_at timestamptz
);
