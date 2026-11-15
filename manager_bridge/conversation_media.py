"""Ephemeral, consented media interpretation before conversation planning.

The authenticated host supplies bytes and registered image/audio callbacks. This
module never downloads URLs, stores material, grants confirmation or executes a
business tool. Extracted language remains untrusted even when it says 'approve'.
"""
from taesan_core.adapters import Media, MediaContext
from taesan_core.core import AccessDenied, Attachment, CoreError, Principal, Reply


def prepare_media(principal, attachments, question, media=None):
    """Keep the current question and every media observation in separate fields.

    An empty tuple is a text-only request. All attachment validation completes
    before the first provider call, so partial malformed batches disclose nothing.
    The caller must apply media_gate before invoking a planner or business tool.
    """
    if not isinstance(principal, Principal):
        raise AccessDenied('authenticated principal required')
    if type(question) is not str or len(question) > 12000:
        raise CoreError('bounded accompanying question required')
    if type(attachments) is not tuple or len(attachments) > 5:
        raise CoreError('bounded consented attachment tuple required')
    if media is not None and not isinstance(media, Media):
        raise CoreError('registered media boundary required')
    if not all(isinstance(item, Attachment) and item.consent is True for item in attachments):
        raise AccessDenied('consented authenticated attachments required')
    if not attachments:
        return None
    return (media or Media()).interpret(principal, attachments, question=question)


def media_gate(context):
    """Return a fixed non-executing reply when any required extraction is absent."""
    if context is None:
        return None
    if not isinstance(context, MediaContext):
        raise CoreError('typed media context required')
    states = {observation.status for observation in context.observations}
    kinds = {observation.kind for observation in context.observations if observation.status != 'ok'}
    if states == {'ok'}:
        return None
    label = '사진·음성' if len(kinds) > 1 else '사진' if kinds == {'image'} else '음성'
    status = 'unavailable' if 'unavailable' in states else 'uncertain'
    message = label + (' 인식 연결이 없습니다.' if status == 'unavailable' else ' 내용을 확인하지 못했습니다.')
    return Reply(status, message + ' 내용을 글로 알려주십시오.',
                 {'media_state': 'not_configured' if status == 'unavailable' else 'failed',
                  'execution_started': False, 'confirmation_granted': False})
