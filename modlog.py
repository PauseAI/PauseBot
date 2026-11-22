"""Builds the posts PauseBot makes in the moderators' log channel (MESSAGE_LOG_CHANNEL_ID) when a
message is edited or deleted, from what the message log in Postgres holds."""
import datetime
import discord

# Discord's limits: an embed's description holds 4096 characters, one embed 6000 in all, and one
# message up to 10 embeds that together hold at most 6000. Discord's client counts in UTF-16 code
# units (an emoji can be two), so everything here does too, to be safe either way.
DESCRIPTION_LIMIT = 4096
MESSAGE_EMBED_CHARS = 6000
MESSAGE_EMBEDS = 10
# Room left for the header lines (author, channel, link) in an edit's description
EDIT_TEXT_LIMIT = 1800
DELETE_TEXT_LIMIT = 2800
ATTACHMENTS_LIMIT = 1000

EDIT_COLOUR = discord.Colour.orange()
DELETE_COLOUR = discord.Colour.red()

FOOTER_PREFIX = "Message ID "

# Log posts never ping, whatever the text they quote
NO_MENTIONS = discord.AllowedMentions.none()


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def cut_utf16(text, units):
    """The longest start of text that fits in `units` UTF-16 code units, never splitting a character."""
    n = 0
    for i, ch in enumerate(text):
        n += 2 if ord(ch) > 0xFFFF else 1
        if n > units:
            return text[:i]
    return text


def truncation_marker(text):
    return f"\n… (truncated, {len(text)} characters in all)"


def truncate(text, limit):
    """text cut to at most `limit` UTF-16 units, saying how much was left out."""
    if utf16_len(text) <= limit:
        return text
    marker = truncation_marker(text)
    return cut_utf16(text, limit - utf16_len(marker)) + marker


FENCE = "```"


def quoted(text, limit):
    """Message text in a code block, so its own markdown, mentions and line breaks can't pass for
    the post's own lines; or a note that it was empty (e.g. attachments only). Backticks get a
    zero-width space after them so the text can't close the block. Within `limit` UTF-16 units."""
    if not text:
        return "*(no text)*"
    body = text.replace("`", "`\u200b")
    room = limit - utf16_len(FENCE + "\n" + "\n" + FENCE)
    if utf16_len(body) <= room:
        return f"{FENCE}\n{body}\n{FENCE}"
    marker = truncation_marker(text)
    body = cut_utf16(body, room - utf16_len(marker))
    # Never end the block mid-escape: a lone backtick before the fence would join it
    if body.endswith("`"):
        body = body[:-1]
    return f"{FENCE}\n{body}\n{FENCE}{marker}"


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
        names = discord.utils.escape_markdown(", ".join(names))
        lines.append(f"**Attachments:** {truncate(names, ATTACHMENTS_LIMIT)}")
    lines += ["", quoted(row["content"], DELETE_TEXT_LIMIT)]
    return discord.Embed(
        title="Message deleted", description="\n".join(lines), colour=DELETE_COLOUR, timestamp=deleted_at,
    ).set_footer(text=f"{FOOTER_PREFIX}{row['message_id']}")


def unlogged_embed(count, channel_id, deleted_at, parent_id=None):
    """Bulk-deleted messages neither the log nor discord.py's cache has."""
    return discord.Embed(
        title=f"{count} more message{'s' if count != 1 else ''} deleted",
        description=f"**Channel:** {where(channel_id, parent_id)}\nTheir text isn't known: they're not in the "
                    "message log (sent over 30 days ago, while PauseBot was down, or by PauseBot itself).",
        colour=DELETE_COLOUR, timestamp=deleted_at,
    )


def embed_size(embed):
    """An embed's length as Discord counts it toward the 6000 limit, in UTF-16 units."""
    return sum(utf16_len(t or "") for t in (embed.title, embed.description, embed.footer.text))


def chunk(items):
    """Groups (embed, sent_at) items into what one message can carry each. A message only holds
    items whose quoted messages were sent on the same UTC day, as the daily prune deletes a post
    when its oldest quoted message passes retention: that way no post goes more than a day early."""
    groups, current, size = [], [], 0
    for item in items:
        n = embed_size(item[0])
        if current and (len(current) == MESSAGE_EMBEDS or size + n > MESSAGE_EMBED_CHARS
                        or item[1].date() != current[0][1].date()):
            groups.append(current)
            current, size = [], 0
        current.append(item)
        size += n
    if current:
        groups.append(current)
    return groups
