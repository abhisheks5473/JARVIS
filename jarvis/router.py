"""Decisions made in Python, because Python calls are free.

Three jobs, all of them things people commonly pay a model to do:

1. **Fast path.** "What time is it" does not need a language model. A handful
   of exact phrases handled locally saves more requests than any amount of
   clever prompting, and answers instantly.

2. **Difficulty routing.** Default to Flash-Lite; escalate to Flash only when
   the task actually needs it. Deciding this with a keyword heuristic costs
   zero requests. Deciding it with a model call costs one -- which means the
   router would spend a request to save a request.

3. **Tool-set selection.** Pick the smallest profile that can plausibly do the
   job, so the model chooses from twelve tools rather than thirty-four.

The heuristics are deliberately blunt. A wrong guess costs a slightly-too-
cheap or slightly-too-expensive call, not a wrong answer -- the model still
does the actual work.
"""
from __future__ import annotations

import re

from .config import ModelTier, Models

# ---------------------------------------------------------------- fast path
# Only phrases whose answer is fully determined locally. Anything with a hint
# of ambiguity goes to the model; a wrong instant answer is worse than a
# correct slow one.
_GREETINGS = {
    "hi", "hey", "hello", "yo", "jarvis", "hey jarvis", "hello jarvis",
    "you there", "you awake", "are you there",
}
_THANKS = {"thanks", "thank you", "cheers", "ta", "thanks jarvis", "nice one"}
_DISMISS = {
    "nothing", "never mind", "nevermind", "forget it", "no worries", "ignore that",
}

_TIME_ONLY = re.compile(
    r"^(what(?:'s| is)? the )?time( is it)?$|^what time is it$", re.I
)
_DATE_ONLY = re.compile(
    r"^(what(?:'s| is)? (the )?date|what day is it|what(?:'s| is)? today)$", re.I
)


def fast_path(text: str) -> str | None:
    """Answer without touching the API, or return None to go to the model."""
    cleaned = text.strip().lower().rstrip("!.?")
    if not cleaned:
        return None

    if cleaned in _GREETINGS:
        return "Sir."
    if cleaned in _THANKS:
        return "Of course."
    if cleaned in _DISMISS:
        return "As you like."

    # Time and date come from the tool, not the model, so answering here is
    # exactly as correct and costs nothing.
    if _DATE_ONLY.match(cleaned):
        from .tools.time_tools import get_time

        return f"It is {get_time()['spoken'].split(',')[0]}."
    if _TIME_ONLY.match(cleaned):
        from .tools.time_tools import get_time

        return f"It is {get_time()['spoken'].split(', ')[-1]}."

    return None


# ---------------------------------------------------------------- difficulty
_COMPLEX_SIGNALS = re.compile(
    r"\b(write|refactor|debug|implement|design|plan|analyse|analyze|compare|"
    r"explain why|figure out|work out|research|investigate|summari[sz]e|"
    r"strategy|architecture|trade-?off|pros and cons|step by step|"
    r"troubleshoot|optimi[sz]e|review)\b",
    re.I,
)
_CODE_SIGNALS = re.compile(
    r"\b(function|class|regex|sql|stack trace|traceback|exception|compile|"
    r"typescript|python|javascript|api|endpoint|schema|bug)\b",
    re.I,
)
_MULTI_STEP = re.compile(
    r"\b(and then|after that|first.*then|also|as well as|both)\b", re.I
)


def choose_tier(text: str, history_turns: int = 0) -> ModelTier:
    """Pick a model tier from the request alone. Costs nothing."""
    if re.search(r"\b(think (hard|carefully)|deeply|thorough(ly)?)\b", text, re.I):
        return Models.DEEP

    score = 0
    if _COMPLEX_SIGNALS.search(text):
        score += 2
    if _CODE_SIGNALS.search(text):
        score += 2
    if _MULTI_STEP.search(text):
        score += 1
    if len(text.split()) > 45:
        score += 1
    # A long conversation is usually one that has got harder, not easier.
    if history_turns > 14:
        score += 1

    return Models.SMART if score >= 3 else Models.FAST


# ---------------------------------------------------------------- tool set
_PROFILE_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    ("recovery", re.compile(
        r"\b(trash|restore|recover|deleted it|undelete|recycle bin)\b", re.I)),
    # Google in the browser. Before briefing, which would otherwise claim
    # "inbox" for the read-only OAuth tools that cannot send anything.
    ("google", re.compile(
        r"\b(gmail|google\s+(drive|docs|sheets|slides|photos|keep|tasks|contacts)|my\s+drive)\b|"
        r"\b(send|write|draft|compose|reply\s+to)\b[^\n]{0,24}\be-?mails?\b|"
        r"\be-?mails?\b[^\n]{0,24}\b(from|to|about|inbox|saying)\b|"
        r"\b(check|read|open|search)\b[^\n]{0,16}\b(inbox|e-?mails?|drive)\b",
        re.I)),
    ("briefing", re.compile(
        r"\b(calendar|schedule|appointment|inbox|unread|briefing|agenda)\b", re.I)),
    # Messaging and calls. Before "control", which would otherwise claim the
    # word "call", and before "create", which would claim "video".
    ("whatsapp", re.compile(
        r"\bwhats\.?\s?app\b|"
        r"\b(decline|reject|ignore|silence|block)\b[^\n]{0,28}\bcalls?\b|"
        r"\bcalls?\b[^\n]{0,28}\b(decline|reject|ignore|silence)\b|"
        r"\b(message|text|msg|dm)\b[^\n]{0,24}\b(him|her|them|saying|that)\b|"
        r"\b(who\s+(has\s+)?called|call\s+log|missed\s+calls?|declined\s+calls?)\b|"
        r"\b(video|voice)\s+calls?\b|"
        r"\b(call|ring|phone)\s+(?!the\b|a\b|an\b|it\b|this\b|that\b|back\b|out\b|off\b|function\b|method\b|api\b|endpoint\b|script\b|command\b)[\w'-]+",
        re.I)),
    # Producing a file.
    ("create", re.compile(
        r"\b(pdf|docx?|word\s+doc|excel|xlsx|spreadsheet|powerpoint|pptx|"
        r"slideshow|deck|mp3|mp4|mkv|wav|m4a|gif|audio|video|narration|voiceover|"
        r"thumbnail|resize|report|invoice|certificate|img|jpe?g|png|webp)\b|"
        r"\b(generate|make|create|draw|design|paint)\b[^\n]{0,20}\b(image|picture|photo|logo|wallpaper|artwork|art|drawing|illustration|poster|icon|avatar)\b|"
        r"\b(image|picture|photo|logo|wallpaper|illustration)\b[^\n]{0,16}\b(of|showing|with)\b|"
        r"\b(convert|export|save)\b[^\n]{0,20}\b(to|into|as)\b", re.I)),
    ("dev", re.compile(
        r"\b(git|commit|branch|repo|pytest|build|compile|stack trace|"
        r"traceback|lint|shell|powershell|script)\b", re.I)),
    ("control", re.compile(
        r"\b(click|double.?click|right.?click|drag|scroll|hover|cursor|"
        r"pointer|mouse|button|checkbox|dropdown|text ?box|"
        r"type\s+(it|in|into|out|the)|press\s+\w+|select\s+the|fill\s+in)\b",
        re.I)),
    ("desk", re.compile(
        r"\b(open|launch|close|window|volume|louder|quieter|mute|play|pause|"
        r"skip|next track|clipboard|lock|spotify|chrome|process|"
        r"task manager|cpu|ram|memory|battery)\b", re.I)),
    ("research", re.compile(
        r"\b(search|look up|find out|who is|latest|news|article|"
        r"website|url|http)\b", re.I)),
]


# A filename is the single strongest signal that file tools are needed, and
# it must never be read as topic vocabulary. "meeting-notes.txt" once routed a
# turn to the calendar profile, which does not contain read_file, so the model
# improvised with fetch_url and web_search and never opened the file at all.
# Filenames are therefore stripped before profile matching, and separately
# force the file tools into the offered set.
_FILENAME = re.compile(
    r"\b[\w\-]+\.(txt|md|py|js|ts|tsx|jsx|json|jsonl|ya?ml|csv|log|ini|cfg|toml|"
    r"html|css|sql|sh|ps1|bat|rst|c|h|cpp|go|rs|rb|php|java|kt|swift|env)\b",
    re.I,
)
_FILE_WORDS = re.compile(
    r"\b(file|folder|directory|workspace|document|script|read|open it|save|"
    r"write|rename|contents of)\b",
    re.I,
)
_FILE_TOOLS = {"read_file", "list_directory", "search_files"}


def required_tools(text: str) -> set[str]:
    """Tools the turn plainly needs, whatever profile was chosen.

    Profiles are a floor, not a ceiling. Selecting one must never be able to
    remove a capability the request obviously requires.
    """
    if _FILENAME.search(text) or _FILE_WORDS.search(text):
        return set(_FILE_TOOLS)
    return set()


def choose_profile(text: str) -> str:
    """Pick the smallest plausible tool set. Falls back to the default."""
    # Match on the text with filenames removed, so an extension or a word
    # inside a filename cannot vote for an unrelated profile.
    topic = _FILENAME.sub(" ", text)
    for name, pattern in _PROFILE_SIGNALS:
        if pattern.search(topic):
            return name
    return "default"


# ---------------------------------------------------------------- category
# What kind of work a request is, so it can be sent to whichever provider is
# kept for that kind. Regex rather than a model call, for the same reason the
# tier and profile choices are: asking a model which model to ask would spend
# a request to save a request.
#
# Order matters. A question can be about several things at once, and the more
# specific reading wins -- "write a python script to price options" is
# programming, not finance, because the thing being produced is code.
_CATEGORY_SIGNALS: list[tuple[str, str]] = [
    ("programming",
     r"\b(code|coding|program|programming|function|class|variable|debug|"
     r"bug|refactor|compile|syntax|regex|api|endpoint|database|sql|query|"
     r"python|javascript|typescript|java|rust|golang|c\+\+|html|css|"
     r"git|repo|commit|stack trace|traceback|exception|unit test|algorithm|"
     r"script|library|framework|deploy|docker|kubernetes)\b"),
    ("maths",
     r"\b(calculate|equation|integral|derivative|matrix|algebra|calculus|"
     r"probability|theorem|proof|geometry|arithmetic|solve for|factorial|"
     r"logarithm|sum of|multiply|divide)\b"),
    ("finance",
     r"\b(invest|investment|stock|shares|portfolio|market|trading|revenue|"
     r"profit|margin|tax|budget|salary|loan|mortgage|interest rate|"
     r"valuation|cash flow|balance sheet|expense|accounting|crypto|"
     r"inflation|dividend|savings|fund|etf|pension|retirement|debt|"
     r"bonds|equity|asset|bank|net worth|compound interest)\b"),
    ("science",
     r"\b(physics|chemistry|biology|molecule|atom|protein|dna|genome|"
     r"quantum|thermodynamic|evolution|species|neuron|enzyme|reaction|"
     r"hypothesis|experiment|astronomy|planet|galaxy|climate|geology|"
     r"photosynthesis|cell|organism|mitochondria|bacteria|virus|"
     r"gravity|orbit|element|compound|catalyst|ecosystem|fossil)\b"),
    ("roleplay",
     r"\b(roleplay|role.play|pretend to be|act as|in character|"
     r"you are now a|persona|story about|write a story|dialogue between|"
     r"improvise|fictional)\b"),
    ("writing",
     r"\b(write|draft|rewrite|edit|proofread|essay|article|blog|poem|"
     r"letter|email|caption|headline|summar|paraphrase|translate|tone)\b"),
    ("research",
     r"\b(research|find out|look up|compare|sources|evidence|study|"
     r"according to|latest|news|who is|what is the difference)\b"),
]

_CATEGORY_PATTERNS = [
    (name, re.compile(pattern, re.I)) for name, pattern in _CATEGORY_SIGNALS
]


def categorise(text: str) -> str:
    """What kind of work this is. "general" when nothing specific matches."""
    if not text or not text.strip():
        return "general"
    # Filenames carry extensions that read as topics; strip them first, the
    # same way profile matching does.
    topic = _FILENAME.sub(" ", text)
    for name, pattern in _CATEGORY_PATTERNS:
        if pattern.search(topic):
            return name
    return "general"
