"""Bumble L2CAP fixes needed to talk to real phones."""
from __future__ import annotations

import dataclasses

from bumble import l2cap

FLUSH_TIMEOUT = l2cap.L2CAP_Configure_Request.ParameterType.FLUSH_TIMEOUT


def accept_flush_timeout() -> None:
    """Accept the Flush Timeout option an iPhone sends when it opens the A2DP media channel.

    Bumble answers it with FAILURE_UNKNOWN_OPTIONS. The iPhone then repeats the same
    configuration until it closes the channel, so A2DP is configured but never streams and
    the phone keeps its audio on the speaker. The option only tells how long the phone keeps
    its own packets before flushing them, so a sink accepts it without changing anything on
    its side. Safe to call more than once.
    """
    original = l2cap.ClassicChannel.on_configure_request
    if getattr(original, "accepts_flush_timeout", False):
        return

    def on_configure_request(self: l2cap.ClassicChannel, request: l2cap.L2CAP_Configure_Request) -> None:
        options = l2cap.L2CAP_Control_Frame.decode_configuration_options(request.options)
        kept = [option for option in options if option[0] != FLUSH_TIMEOUT]
        if len(kept) != len(options):
            request = dataclasses.replace(
                request, options=l2cap.L2CAP_Control_Frame.encode_configuration_options(kept))
        original(self, request)

    on_configure_request.accepts_flush_timeout = True  # type: ignore[attr-defined]
    l2cap.ClassicChannel.on_configure_request = on_configure_request  # type: ignore[method-assign]
