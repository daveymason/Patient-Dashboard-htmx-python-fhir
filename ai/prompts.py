"""System prompts and message assembly.

Prompts are **versioned**, and the version is written to `ai_exchange` beside
every response. When someone reports a bad answer six weeks from now, the
prompt will have been edited several times since; without the version stamped
on the record, nobody can tell which wording produced it, and the investigation
has nothing to stand on.

Message assembly is deliberately rigid. The system prompt and the patient
context are always the first two messages, always byte-identical for a given
patient and feature, and the conversation is appended after them. That ordering
is what makes provider-side prompt caching hit: caching keys on an exact
prefix, so one reordered message turns a cached 19K-token context (at a tenth
of the price) back into a fresh one on every turn.
"""
import hashlib
import json

# Bump on any change to the wording below. Dated rather than sequential so the
# record says when a prompt was in force, not merely that it differed.
PROMPT_VERSION = '2026-08-09.1'

# Applies to every feature. Separated from the per-feature text so a change to
# the safety rules cannot be made in one feature and forgotten in the others.
_SHARED_RULES = """\
You are assisting a qualified clinician who is reading a patient's record. You
are not talking to the patient, and you are not making clinical decisions.

Rules, in order of precedence:

1. Use only the patient context supplied below. It is the whole of what you
   know about this patient. Your own general knowledge may explain what a term
   means, but it may never supply a fact about this patient.
2. Cite the source of every clinical claim with the tag shown on the line you
   took it from, like [Condition/1234]. A claim without a tag will be read as
   something you invented.
3. If the context does not answer the question, say so plainly and say what
   would answer it. Do not estimate, infer a likely value, or fill a gap with
   what is typical. "The record does not say" is a useful answer.
4. Absent is not negative. If there are no allergies listed, say the record
   lists none -- never that the patient has no allergies.
5. Flag contradictions and stale data rather than resolving them silently: an
   active condition with no recent review, two medications that interact, a
   result far outside its reference range.
6. Do not diagnose, do not prescribe, and do not recommend a dose. You may
   describe what the record shows and what a clinician might want to check.
7. Be brief. A clinician is reading this between patients."""

_FEATURE_PROMPTS = {
    'summary': """\
Summarise this patient for a clinician who has not met them before, in under
200 words. Lead with anything that changes immediate management: allergies,
active problems, current medication. Then a short history. Finish with up to
three things worth checking, each tied to what in the record prompted it.""",

    'chat': """\
Answer the clinician's questions about this patient's record. Keep answers
short and specific. Quote the values you are reasoning from.""",

    'section': """\
Answer questions about one section of this patient's record. The context holds
that section only, so be explicit that you cannot see the rest of the record
rather than reasoning as though the rest is empty.""",
}

FEATURES = tuple(sorted(_FEATURE_PROMPTS))


def system_prompt(feature):
    """The full system prompt for `feature`, and the version it came from.

    Returns (version, text). Raises ValueError on an unknown feature: silently
    falling back to a generic prompt would mean the safety rules a feature was
    reviewed under are not the ones that ran.
    """
    if feature not in _FEATURE_PROMPTS:
        raise ValueError(
            f'Unknown prompt feature: {feature!r}. Known: {", ".join(FEATURES)}'
        )
    return PROMPT_VERSION, f'{_SHARED_RULES}\n\n{_FEATURE_PROMPTS[feature]}'


def context_message(context_text):
    """Wrap a context block as the second message of every conversation."""
    return {
        'role': 'user',
        'content': (
            'Patient context follows. Every line ends with the resource it came '
            'from; cite those tags.\n\n'
            f'{context_text}'
        ),
    }


def build_messages(feature, context_text, user_message=None, history=()):
    """Assemble the message list for one request.

    `history` is prior (role, content) turns, oldest first, and is appended
    after the fixed prefix so the cached portion never moves. Returns
    (messages, version).
    """
    version, system = system_prompt(feature)

    messages = [
        {'role': 'system', 'content': system},
        context_message(context_text),
    ]
    for role, content in history:
        if role not in ('user', 'assistant') or not content:
            continue
        messages.append({'role': role, 'content': content})
    if user_message:
        messages.append({'role': 'user', 'content': user_message})

    return messages, version


def cacheable_prefix(messages):
    """The leading messages that stay byte-identical across a conversation.

    Exposed so a test can assert the property directly rather than inferring it
    from a cache-hit rate nobody can observe offline.
    """
    return messages[:2]


def prompt_hash(messages):
    """Stable digest of an assembled prompt, for the `ai_exchange` record.

    The prompt itself is not stored: it contains the patient's record, and the
    record already lives on the FHIR server. The hash is enough to prove two
    exchanges used the same prompt, or that one did not.
    """
    payload = json.dumps(messages, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
