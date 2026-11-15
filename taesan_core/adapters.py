"""Host-registered platform boundaries; no network access or credential storage.

This module is an SDK boundary, not a working Kakao/ERP authentication provider.
The host must authenticate every call, authorize the conversation, and verify an
explicit user confirmation action independently of model output. Media callbacks
receive consented bytes already downloaded by that host, never arbitrary URLs.
"""
from dataclasses import dataclass

from .core import (AccessDenied, Attachment, Core, CoreError, Evidence, Plan,
                   Principal, Reply, encoded, ident)


@dataclass(frozen=True)
class Platform:
    """Trusted application configuration, not fields accepted from a request.

    authenticate(credential, conversation) returns the *current* Principal after
    verifying both authentication and access to the conversation, or raises.
    authorize_confirmation(principal, credential, conversation, confirmation)
    must verify the host's explicit user UI action/CSRF protection. It must not
    accept a model response, OCR text, or a transcript as approval.
    """
    channel: str
    authenticate: object
    enabled: bool = False
    authorize_confirmation: object = None

    def __post_init__(self):
        ident(self.channel)
        if not callable(self.authenticate) or type(self.enabled) is not bool:
            raise CoreError('invalid platform configuration')
        if self.authorize_confirmation is not None and not callable(self.authorize_confirmation):
            raise CoreError('invalid confirmation authorizer')


def decode_plan(value):
    """Convert untrusted planner JSON to a proposal with no approval authority."""
    if type(value) is not dict or not {'tool'} <= value.keys() or not value.keys() <= {'tool', 'arguments', 'follow_up'}:
        raise CoreError('invalid plan envelope')
    return Plan(value['tool'], value.get('arguments', {}), value.get('follow_up', False))


class Platforms:
    """Hosts install supported channels explicitly; all registrations default off."""
    def __init__(self, core):
        if not isinstance(core, Core):
            raise CoreError('business core required')
        self.core = core
        self._platforms = {}

    def register(self, platform):
        if not isinstance(platform, Platform) or platform.channel in self._platforms:
            raise CoreError('duplicate or invalid platform')
        self._platforms[platform.channel] = platform

    def handle(self, channel, credential, body):
        """Dispatch JSON after fresh host authentication; never trust client roles.

        Body is exactly {request_id, conversation, plan} or
        {request_id, conversation, confirmation}. Confirmations additionally
        require a registered host action verifier. A disconnected platform does
        not invoke authentication, tools, or media providers.
        """
        ident(channel)
        platform = self._platforms.get(channel)
        if platform is None or not platform.enabled:
            return Reply('unavailable', '이 플랫폼은 아직 연결되지 않았습니다.')
        if type(body) is not dict:
            raise CoreError('invalid request envelope')
        if set(body) not in ({'request_id', 'conversation', 'plan'},
                             {'request_id', 'conversation', 'confirmation'}):
            raise CoreError('invalid request envelope')
        request_id, conversation = ident(body['request_id']), ident(body['conversation'])
        try:
            principal = platform.authenticate(credential, conversation)
        except Exception:
            return Reply('denied', '로그인 또는 대화 접근 권한을 확인해 주십시오.')
        if not isinstance(principal, Principal):
            return Reply('denied', '로그인 또는 대화 접근 권한을 확인해 주십시오.')
        if 'confirmation' in body:
            confirmation = ident(body['confirmation'])
            if platform.authorize_confirmation is None:
                return Reply('denied', '사용자의 명시적 변경 확인을 검증할 연결이 없습니다.')
            try:
                approved = platform.authorize_confirmation(
                    principal, credential, conversation, confirmation)
            except Exception:
                approved = False
            if approved is not True:
                return Reply('denied', '변경 확인 요청을 검증하지 못했습니다.')
            return self.core.handle(principal, channel, conversation, request_id,
                                    confirm=confirmation)
        return self.core.handle(principal, channel, conversation, request_id,
                                plan=decode_plan(body['plan']))


@dataclass(frozen=True)
class Extracted:
    """Untrusted interpretation plus a provider receipt, never execution evidence."""
    text: str
    evidence: Evidence

    def __post_init__(self):
        if type(self.text) is not str or not self.text.strip() or len(self.text) > 12000:
            raise CoreError('invalid extraction')
        if not isinstance(self.evidence, Evidence):
            raise CoreError('extraction receipt required')


@dataclass(frozen=True)
class MediaObservation:
    status: str
    kind: str
    extraction: Extracted | None = None

    def __post_init__(self):
        if self.status not in {'ok', 'unavailable', 'failed'} or self.kind not in {'image', 'audio'}:
            raise CoreError('invalid media observation')
        if self.status == 'ok' and not isinstance(self.extraction, Extracted):
            raise CoreError('extraction required')
        if self.status != 'ok' and self.extraction is not None:
            raise CoreError('failed media cannot carry extraction')

    def planner_data(self):
        """Explicit data boundary. Do not merge these fields into a user/plan dict."""
        return {'kind': self.kind, 'status': self.status,
                'authority': 'untrusted_business_material',
                'text': self.extraction.text if self.extraction else None}


@dataclass(frozen=True)
class MediaContext:
    question: str
    observations: tuple

    def __post_init__(self):
        if type(self.question) is not str or len(self.question) > 12000:
            raise CoreError('invalid accompanying question')
        if type(self.observations) is not tuple or not 1 <= len(self.observations) <= 5 or not all(
                isinstance(value, MediaObservation) for value in self.observations):
            raise CoreError('invalid media context')

    def planner_data(self):
        """The current question and media are kept distinct, including empty question."""
        value = {'user_question': self.question,
                 'untrusted_material': [value.planner_data() for value in self.observations]}
        encoded(value)
        return value


class Media:
    """No fetching, logging, persistence, approval, or business execution.

    A registered callback(principal, bytes, accompanying_question) returns an
    Extracted value. Missing callbacks are unavailable; exceptions/malformed
    responses are failed. Provider exceptions and raw bytes are never returned.
    A transcript can inform a proposed write, but cannot confirm that write.
    """
    def __init__(self, *, image=None, audio=None):
        for callback in (image, audio):
            if callback is not None and not callable(callback):
                raise CoreError('invalid media provider')
        self._providers = {'image': image, 'audio': audio}

    def interpret(self, principal, attachments, *, question=''):
        if not isinstance(principal, Principal):
            raise AccessDenied('authenticated principal required')
        if type(question) is not str or len(question) > 12000:
            raise CoreError('invalid accompanying question')
        if type(attachments) is not tuple or not 1 <= len(attachments) <= 5:
            raise CoreError('provide one to five attachments')
        # Validate the entire batch before invoking any external callback.
        if not all(isinstance(item, Attachment) and item.consent is True for item in attachments):
            raise AccessDenied('consented authenticated attachments required')
        observations = []
        for attachment in attachments:
            provider = self._providers[attachment.kind]
            if provider is None:
                observation = MediaObservation('unavailable', attachment.kind)
            else:
                try:
                    extraction = provider(principal, attachment.content, question)
                    if not isinstance(extraction, Extracted):
                        raise CoreError('malformed media provider response')
                    observation = MediaObservation('ok', attachment.kind, extraction)
                except Exception:
                    observation = MediaObservation('failed', attachment.kind)
            observations.append(observation)
        return MediaContext(question, tuple(observations))
