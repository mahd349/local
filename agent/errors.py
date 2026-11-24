"""Shared exception types."""


class AgentError(Exception):
    """Base class for all controlled agent failures."""


class TargetNotFound(AgentError):
    """No candidate selector could resolve the target."""


class BlockedByGuard(AgentError):
    """Captcha or login wall was not cleared in time."""


class UserAbort(AgentError):
    """Emergency stop was requested."""


class OutsideSandbox(AgentError):
    """A path outside the allowed write roots was requested."""