"""Separate Codex's internal approval work from user-directed model usage."""

INTERNAL_REVIEW_MODEL = 'codex-auto-review'


def internal_review(session):
    if session.get('source') == 'guardian_review':
        return True
    rows = session.get('history', ())
    if rows:
        return all((row.get('requested_model') or row.get('configured_model') or row.get('model'))
                   == INTERNAL_REVIEW_MODEL for row in rows)
    return session.get('model') == INTERNAL_REVIEW_MODEL


def call_count(priced, calls):
    """Show a ratio only when user-work calls actually lack a price."""
    return f'{priced:,} / {calls:,}' if priced < calls else f'{calls:,}'
