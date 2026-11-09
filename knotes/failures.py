# SPDX-License-Identifier: GPL-3.0-only
"""Plain failure texts for the user: what happened and what to do, without technical details.

Details (stack trace, codes) go to the app log; windows and messages show only this text.
"""

import errno
import subprocess

from knotes import providers, transcript
from knotes.language import tr


def text(error: BaseException) -> str:
    if isinstance(error, (providers.ProviderError, transcript.Busy)):
        return str(error)
    if isinstance(error, subprocess.CalledProcessError):
        return tr("Couldn't process the audio: the file is damaged or its format isn't supported.")
    if isinstance(error, OSError):
        return _disk_or_audio(error)
    return tr("Unexpected error: {error}. Details are in the app log.", error=str(error) or type(error).__name__)


def _disk_or_audio(error: OSError) -> str:
    where = f" ({error.filename})" if error.filename else ""
    if error.errno == errno.ENOSPC:
        return tr("The disk is full{where}. Free up space and try again.", where=where)
    if isinstance(error, PermissionError):
        return tr(
            "Access denied{where}. Check the folder permissions or choose another recordings folder in Settings.",
            where=where,
        )
    if isinstance(error, FileNotFoundError):
        return tr(
            "Not found{where}. If the recordings folder is on a removable or network drive, connect it.", where=where
        )
    if (error.errno or 0) < 0:  # PortAudio: the device is gone or busy
        return tr(
            "Audio device unavailable: check that the microphone and headphones are connected and not used by another "
            "app."
        )
    return tr("Disk or device error: {reason}.", reason=error.strerror or str(error) or type(error).__name__)
