"""Memory, exposed to the model.

`remember` is deceptively important. An assistant that forgets what you told
it last week is a chatbot; one that does not is starting to be an assistant.
The description below is written to make the model reach for it unprompted
when you state a durable preference, and to leave it alone for passing
chatter -- that balance is entirely a prompt-engineering problem, which is
why the wording is fussier than it looks.
"""
from __future__ import annotations

from ..memory.store import memory
from .base import ToolError, tool


@tool(group="core", always_available=True)
def remember(fact: str, category: str = "general", importance: int = 3) -> dict:
    """Store something about the user for future conversations.

    Call this without being asked whenever the user states a durable
    preference, a fact about themselves, how they work, what they are working
    on, or a correction to something you got wrong. "I always use PowerShell"
    is worth remembering. "What time is it" is not.

    Do not store secrets, passwords, or anything they would not want written
    to a file on disk. Do not store the contents of documents; store the fact
    that a document exists and where it is.

    Args:
        fact: One self-contained sentence. It must make sense on its own in
            six months, so write "Abhishek uses PowerShell, not bash", never
            "he prefers the first one".
        category: A loose grouping such as preference, project, person,
            environment, or schedule.
        importance: 1 to 5. Only use 5 for things that should shape every
            conversation.
    """
    if not fact.strip():
        raise ToolError("nothing to remember", hint="give a complete sentence")
    if len(fact) > 500:
        raise ToolError(
            "that is too long to be a single fact",
            hint="split it into separate, self-contained facts",
        )
    return memory.remember(fact, category=category, importance=importance)


@tool(group="core", always_available=True)
def search_memory(query: str, limit: int = 6) -> dict:
    """Search what you have been told about the user.

    The most important facts are already in your context, so use this for
    older or more specific things -- a project detail, a name, something
    mentioned weeks ago. If it returns nothing, say you do not recall rather
    than inventing something plausible.

    Args:
        query: Keywords to search for. This is keyword matching, not semantic,
            so use the words the user would actually have said.
        limit: Maximum results to return.
    """
    hits = memory.search(query, limit=max(1, min(int(limit), 20)))
    return {
        "query": query,
        "facts": [
            {"id": f.id, "fact": f.fact, "category": f.category, "since": f.created_on}
            for f in hits
        ],
        "count": len(hits),
        "note": "nothing stored matches that" if not hits else "",
    }


@tool(group="core")
def forget_fact(fact_id: int) -> dict:
    """Delete a stored fact by its id.

    Use when the user says something you remembered is wrong or out of date.
    Get the id from search_memory first. Prefer storing a corrected fact over
    deleting, unless the old one is actively misleading.

    Args:
        fact_id: The id reported by search_memory.
    """
    if memory.forget(int(fact_id)):
        return {"forgotten": fact_id}
    raise ToolError(
        f"no stored fact with id {fact_id}",
        hint="call search_memory to find the right id",
    )


@tool(group="core", always_available=True)
def recall_conversations(query: str, limit: int = 4) -> dict:
    """Search what you and the user talked about in earlier sessions.

    Relevant past conversations are already put in front of you automatically,
    so reach for this when that was not enough: the user refers to something
    specific you cannot see, asks what was decided, or asks when something was
    discussed. Say when it happened, not just what was said.

    Args:
        query: Words likely to appear in that conversation.
        limit: How many to return.
    """
    if not query.strip():
        raise ToolError(
            "no query given", hint="give a word or two from the conversation"
        )

    found = memory.search_episodes(query, limit=max(1, min(int(limit), 8)))
    if not found:
        return {
            "found": 0,
            "note": (
                "nothing matching in earlier sessions -- say so plainly rather "
                "than guessing at what was discussed"
            ),
        }

    return {
        "found": len(found),
        "conversations": [
            {"when": e["when"], "on": e["on"], "turns": e["turns"],
             "summary": e["summary"]}
            for e in found
        ],
    }
