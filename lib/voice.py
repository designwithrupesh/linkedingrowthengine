"""Shared, deterministic checks for the owner's plain writing preference.

These checks enforce observable punctuation and language rules. They do not
infer authorship, score AI likelihood, or replace a factual/context review.
"""
from __future__ import annotations

import html
import re


class VoiceError(ValueError):
    """Content must be edited before it can leave the writing boundary."""


_WORDS = (
    'leverage', 'utilize', 'facilitate', 'streamline', 'robust', 'seamless',
    'delve', 'navigate', 'unlock', 'harness', 'foster', 'cultivate',
    'fundamentally', 'essentially', 'ultimately', 'crucially', 'notably',
    'landscape', 'ecosystem', 'paradigm', 'realm', 'tapestry', 'journey',
    'synergy', 'transformative', 'revolutionary', 'groundbreaking',
)
_JARGON = re.compile(
    r'\b(?:' + '|'.join(_WORDS) +
    r'|leverag(?:es|ed|ing)|utiliz(?:es|ed|ing|ation)|facilitat(?:es|ed|ing|ion)'
    r'|streamlin(?:es|ed|ing)|delv(?:es|ed|ing)|navigat(?:es|ed|ing)'
    r'|unlock(?:s|ed|ing)|harness(?:es|ed|ing)|foster(?:s|ed|ing)'
    r'|cultivat(?:es|ed|ing|ion)|seamlessly)\b', re.IGNORECASE,
)
_PHRASES = re.compile(
    r"\b(?:in today['’]s (?:fast[- ]paced|ever[- ]evolving|digital) world"
    r'|game[- ]changer|deep dive|at the end of the day|let that sink in'
    r'|read that again|the harsh truth|the uncomfortable truth'
    r'|here(?:\'s| is) (?:the thing|what most people miss)'
    r'|what nobody tells you|now more than ever|take it to the next level'
    r'|the future belongs to|food for thought|true value lies in'
    r'|(?:the|this) post (?:raises|highlights|shows|reminds|underscores|reinforces|captures|points out)'
    r'|key tension|the real question|depth of user value'
    r'|meaningful impact|purposeful and impactful'
    r"|(?:shape|shapes|shaping) (?:your|the) product(?:['’]s)? trajectory)\b"
    r'|\b(?:thoughts|agree|what do you think)\?\s*$',
    re.IGNORECASE,
)
_PRAISE = re.compile(
    r"^\s*(?:a (?:strong|great|useful|timely|powerful) reminder\b"
    r'|great (?:post|point|insights?|perspective)\b|love (?:this|the)\b'
    r'|this is (?:such )?a (?:great|strong|powerful) reminder\b'
    r'|couldn[\'’]t agree more\b|could not agree more\b'
    r'|(?:absolutely|so) (?:true|right)\b|well said\b'
    r'|this resonates\b|spot on\b|100%(?=\s|[.!]|$)|this\.[\s.!]*$'
    r'|thanks for (?:sharing|the insight)[.!\s]*$'
    r'|(?:agree|agreed|exactly|absolutely|true|yes)[.!\s]*$)',
    re.IGNORECASE,
)
_CONTRAST = re.compile(
    r"\b(?:it['’]s|it is|this is|that['’]s|that is) not (?:just|about)\b"
    r"|\b(?:it|this|that) isn['’]t (?:just|about)\b"
    r"|\b(?:it['’]s|it is) not [^\n.!?]{1,100}[.;] (?:it['’]s|it is)\b"
    r"|\b(?:isn['’]t|is not) just [^\n.!?]{1,100}[,;] (?:it['’]s|it is)\b"
    r'|\bnot just [^\n.!?]{1,100}\bbut\b',
    re.IGNORECASE,
)
_SCAFFOLD = re.compile(
    r'(?m)^\s*(?:#{1,6}\s|(?:[-*+] |\d{1,2}[.)] )\S|'
    r'(?:hook|body|cta|caption|key takeaway|bottom line|in conclusion)\s*:)'
    r'|\*\*|__|```|`[^`]+`|\[[^\]]+\]\([^\)]+\)',
    re.IGNORECASE,
)
_EMOJI = re.compile(r'[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]')
# These exact constructions recur in recorded comments. Keep this narrower
# than a ban on ordinary words such as test, watch, useful or trade-off.
_RECYCLED_ADVICE = re.compile(
    r"\b(?:the real trade[- ]off|a key trade[- ]off|that['’]s where authenticity starts"
    r"|i['’]d watch how|what would you test next"
    r'|how do you test if users actually adopt this behaviou?r)\b'
    r"|\bthe post (?:doesn['’]t|does not) say\b"
    r"|^\s*what if\b[^?\n]{1,320}\?\s*i['’]d (?:test|watch|want to see|encourage)\b"
    r'|\b(?:the bigger picture|intuitive and efficient)\b'
    r'|\b(?:a smart way to|a smart move for|without overwhelming the users?)\b'
    r'|\bthat balance between [^.?!]{1,90} helps users\b'
    r'|^\s*the idea that [^.!?\n]{1,160} is (?:powerful|compelling|interesting)\b',
    re.IGNORECASE,
)


def violations(text: str) -> tuple[str, ...]:
    """Return fixed safe diagnostics, never the text or matched phrase."""
    if not isinstance(text, str) or not text.strip():
        return ('Text is empty or invalid',)
    # Entity spelling must not bypass the punctuation rule. JSON escapes have
    # already been decoded by the caller; also catch literal escape strings.
    decoded = html.unescape(text)
    checks = (
        (bool(re.search(r'[\u2012-\u2015]|--|\\u201[2345]', decoded, re.IGNORECASE)),
         'Text contains a prohibited dash'),
        (bool(_JARGON.search(decoded)), 'Text contains stock jargon'),
        (bool(_PHRASES.search(decoded)), 'Text contains a canned phrase'),
        (bool(_RECYCLED_ADVICE.search(decoded)), 'Text repeats a stock advice template'),
        (bool(_PRAISE.search(decoded)), 'Text contains generic praise or an empty agreement'),
        (bool(_CONTRAST.search(decoded)), 'Text uses a formulaic contrast'),
        (bool(_SCAFFOLD.search(decoded)), 'Text contains writing scaffolding or Markdown'),
        (bool(re.search(r'(?<!\w)#[A-Za-z][\w-]*', decoded)), 'Text contains a hashtag'),
        (bool(_EMOJI.search(decoded)), 'Text contains decorative emoji'),
        (bool(re.search(r'\b(?:as an ai|as a language model|here is (?:a|your) (?:draft|post|reply))\b', decoded, re.IGNORECASE)),
         'Text contains assistant commentary'),
    )
    return tuple(reason for failed, reason in checks if failed)


def require_plain_text(text: str) -> str:
    """Reject rather than silently rewrite the meaning before a write."""
    failures = violations(text)
    if failures:
        raise VoiceError('; '.join(failures))
    return text.strip()


def prompt_rules() -> str:
    """Rules included explicitly in initial generation and the one edit pass."""
    return (
        'OWNER WRITING RULES, required for every post, comment, reply and message. '
        'Write as Rupesh, a designer talking to one person, using ordinary words. '
        'Respond to the exact detail or claim the person chose to share. Say what you make of it. '
        'A specific reaction or design judgement is enough: appreciate a particular choice, '
        'agree for a clear reason, disagree with a supported claim, or answer the question asked. '
        'Choose what fits this conversation rather than covering all these moves. '
        'Do not force a lesson, a mechanism, a trade-off, an experiment or consulting advice into every response. '
        'Do not manufacture a disagreement or assume the author ignored something absent from an excerpt. '
        'Let the supplied detail carry the point; avoid turning it into a broad speech about design. '
        'Do not review the announcement like marketing copy. Avoid a feature summary followed by '
        'a generic benefit such as intuitive and efficient, a smart way to, a smart move for, '
        'without overwhelming the user, or seeing the bigger picture. '
        'For a comment or reply, join as a peer with one exact thought in one or two ordinary sentences. '
        'Stop there. Do not give a mini-review, explain the features back to their author, '
        'or follow your reaction with a rundown of benefits. '
        'Do not claim an interface is intuitive, fast or easier, or that it helps users quickly '
        'understand something, without actual evidence in the supplied context. '
        'Express your own preference about a specific label or placement, not an untested claim about users. '
        'Prefer one direct sentence when it answers the person; extend only for another concrete detail. '
        'Use ordinary contractions, plain ASCII punctuation and varied sentence lengths when they sound natural. '
        'Never use em dashes, en dashes, double hyphens or decorative separators. '
        'Use a comma, colon, parentheses or a sentence break instead. '
        'No generic praise, empty agreement, hype, engagement bait or a canned opener. '
        'No "great post", "love this", "a strong reminder", "couldn\'t agree more", '
        '"well said", "spot on", "this resonates", "here\'s the thing", '
        '"let that sink in", "game-changer", "deep dive", or "at the end of the day". '
        'Do not narrate what "the post" raises, highlights, shows, reminds, underscores, '
        'reinforces, captures or points out. Speak directly to the person and their idea. '
        'Avoid "key tension", "the real question", "depth of user value", "true value lies in", '
        '"meaningful impact", "purposeful and impactful", and "shape your product\'s trajectory". '
        'Avoid the repeated templates "the real trade-off", "a key trade-off", '
        '"I\'d watch how", "that\'s where authenticity starts", and "what would you test next?". '
        'Do not default to "What if...? I\'d test/watch..." or turn every reaction into advice '
        'about testing a customer task. Suggest a test only when that suggestion fits the actual discussion. '
        'Do not use "it\'s not X, it\'s Y", "not just X but Y", or "this isn\'t about X" framing. '
        'Avoid stock jargon: ' + ', '.join(_WORDS) + '. '
        'Use plain prose, with no headings, bullets, numbered lists, hashtags, emojis, '
        'bold, code fences, labels such as Hook/CTA, or assistant commentary. '
        'Do not impose a repeated paragraph formula, a rule of three, a dramatic reveal, '
        'a tidy takeaway, a consulting pitch or a question at the end. '
        'Ask a question only when its answer matters to this exact conversation. '
        'One direct sentence can be a complete comment or reply. Stop when the response is complete; '
        'do not pad it to sound helpful or authoritative. Keep names and product names capitalized. '
        'Do not recycle an opening, a hypothetical scenario or a concluding question from recent comments. '
        'Do not imitate spelling mistakes, fake hesitation, confessions or vulnerability to sound personal. '
        'Preserve supplied facts and never invent personal stories, relationships, results or numbers. '
        'If the context is too thin to respond truthfully, skip rather than filling gaps. '
        'These are writing preferences, not a claim about who authored the text.'
    )
