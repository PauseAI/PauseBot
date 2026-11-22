"""Builds the posts PauseBot makes in the moderators' log channel (MESSAGE_LOG_CHANNEL_ID) when a
message is edited or deleted, from what the message log in Postgres holds."""
import datetime
import discord

# Discord's limits: an embed's description holds 4096 characters, one embed 6000 in all, and one
# message up to 10 embeds that together hold at most 6000.
DESCRIPTION_LIMIT = 4096
MESSAGE_EMBED_CHARS = 6000
MESSAGE_EMBEDS = 10
# Room left for the header lines (author, channel, link) in an edit's description
EDIT_TEXT_LIMIT = 1800
DELETE_TEXT_LIMIT = 2800
ATTACHMENTS_LIMIT = 1000

EDIT_COLOUR = discord.Colour.orange()
DELETE_COLOUR = discord.Colour.red()

# Each post names the message it quotes here, which the daily prune reads back
FOOTER_PREFIX = "Message ID "

# Log posts never ping, whatever the text they quote
NO_MENTIONS = discord.AllowedMentions.none()


def truncate(text, limit):
    """text cut to at most `limit` characters, saying how much was left out."""
    if len(text) <= limit:
        return text
    # Reserve room for the marker itself, sized for the largest count it could show
    marker = f"\n… (truncated, {len(text)} characters in all)"
    return text[:limit - len(marker)] + marker


def quoted(text, limit):
    """Message text for an embed, or a note that it was empty (e.g. attachments only)."""
    return truncate(text, limit) if text else "*(no text)*"


def jump_url(guild_id, channel_id, message_id):
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def where(channel_id, parent_id=None):
    """The channel, or the thread and its parent channel."""
    return f"<#{channel_id}> in <#{parent_id}>" if parent_id else f"<#{channel_id}>"


def timestamp(when):
    """Discord renders this in each viewer's own time zone."""
    return f"<t:{int(when.timestamp())}:f>"


def header(author_id, channel_id, parent_id):
    return f"**Author:** <@{author_id}> (`{author_id}`)\n**Channel:** {where(channel_id, parent_id)}"


def edit_embed(*, guild_id, channel_id, message_id, author_id, before, after, edited_at, parent_id=None):
    description = "\n".join([
        header(author_id, channel_id, parent_id),
        f"**Message:** {jump_url(guild_id, channel_id, message_id)}",
        "", "**Before**", quoted(before, EDIT_TEXT_LIMIT),
        "", "**After**", quoted(after, EDIT_TEXT_LIMIT),
    ])
    return discord.Embed(
        title="Message edited", description=description, colour=EDIT_COLOUR,
        timestamp=edited_at or datetime.datetime.now(datetime.UTC),
    ).set_footer(text=f"{FOOTER_PREFIX}{message_id}")


def delete_embed(row, deleted_at, parent_id=None):
    """One deleted message, from the message_log row log_delete returned."""
    lines = [
        header(row["author_id"], row["channel_id"], parent_id),
        f"**Sent:** {timestamp(row['created_at'])}",
    ]
    names = [a.get("filename") or "(unnamed)" for a in row["attachments"] or []]
    if names:
        lines.append(f"**Attachments:** {truncate(', '.join(names), ATTACHMENTS_LIMIT)}")
    lines += ["", quoted(row["content"], DELETE_TEXT_LIMIT)]
    return discord.Embed(
        title="Message deleted", description="\n".join(lines), colour=DELETE_COLOUR, timestamp=deleted_at,
    ).set_footer(text=f"{FOOTER_PREFIX}{row['message_id']}")


def unlogged_embed(count, channel_id, deleted_at, parent_id=None):
    """Bulk-deleted messages the log has nothing on (sent over 30 days ago, or while the bot was down)."""
    return discord.Embed(
        title=f"{count} more message{'s' if count != 1 else ''} deleted",
        description=f"**Channel:** {where(channel_id, parent_id)}\nNot in the message log, so their text isn't known.",
        colour=DELETE_COLOUR, timestamp=deleted_at,
    )


def chunk(embeds):
    """Groups embeds into the lists one message can carry each."""
    groups, current, size = [], [], 0
    for embed in embeds:
        n = len(embed)
        if current and (len(current) == MESSAGE_EMBEDS or size + n > MESSAGE_EMBED_CHARS):
            groups.append(current)
            current, size = [], 0
        current.append(embed)
        size += n
    if current:
        groups.append(current)
    return groups



def oldest_quoted(post):
    """When the oldest message a log post quotes was sent (from the message IDs in its footers),
    or when the post itself was made if that's older or it quotes none."""
    times = [post.created_at]
    for embed in post.embeds:
        text = embed.footer.text or ""
        if text.startswith(FOOTER_PREFIX) and text[len(FOOTER_PREFIX):].isdigit():
            times.append(discord.utils.snowflake_time(int(text[len(FOOTER_PREFIX):])))
    return min(times)
