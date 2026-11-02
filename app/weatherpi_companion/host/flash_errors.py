"""Errors from the SD card flasher, with plain-words messages for the owner."""

from __future__ import annotations

import errno

from ..checks import Action, CheckResult

#: code -> message for the owner
MESSAGES: dict[str, str] = {
    "cancelled": "Flashing was stopped. The card can't be used until it's flashed again.",
    "card_removed": (
        "The SD card was removed or stopped responding. Put it back in and try again."
    ),
    "card_changed": (
        "The cards in this PC changed since you chose one. Please choose the card again."
    ),
    "not_allowed": (
        "For safety, the app won't write to that drive. Only SD cards in a card reader "
        "can be flashed."
    ),
    "write_protected": (
        "The SD card is locked (write-protected). Take it out, slide the small switch on "
        "its side away from \"Lock\", put it back and try again."
    ),
    "too_small": "This SD card is too small for WeatherPi. Use a card of 8 GB or more.",
    "access_denied": (
        "Windows didn't let the app write to the card. Close any windows that show the "
        "card's files, then try again and answer Yes when Windows asks for permission."
    ),
    "card_in_use": (
        "Another program is using the SD card. Close any windows showing its files, then "
        "try again."
    ),
    "uac_cancelled": (
        "Windows asked for permission and it wasn't given. The app needs it to write to "
        "the SD card. Try again and answer Yes."
    ),
    "verify_failed": (
        "The card didn't read back correctly after writing, so it may be faulty. Try "
        "again, and if it fails again use a different SD card."
    ),
    "write_failed": (
        "Writing to the SD card failed. Re-insert the card and try again; if it keeps "
        "failing, use a different SD card."
    ),
    "sha_mismatch": "The WeatherPi image file is damaged or incomplete. Download it again.",
    "image_changed": "The WeatherPi image file changed while it was being written. Try again.",
    "image_bad": "That file isn't a complete WeatherPi image. Download it again.",
    "image_missing": "The WeatherPi image file couldn't be found or opened.",
    "no_sha_file": (
        "The image's checksum file (.sha256) is missing. Keep it in the same folder as the "
        "image, or download the image in the app instead."
    ),
    "bad_sha_file": (
        "The image's checksum file (.sha256) doesn't match the image. Download the image "
        "again."
    ),
    "no_image_published": (
        "There's no WeatherPi image to download yet. If someone gave you an image file, "
        "choose it with \"Use a file on this PC\"."
    ),
    "offline": (
        "Couldn't reach GitHub to download the image. Check this PC's internet connection "
        "and try again."
    ),
    "download_failed": (
        "The download didn't finish. Check this PC's internet connection and try again; it "
        "carries on where it stopped."
    ),
    "disk_full": "This PC's disk is full. Free up about 1 GB and try again.",
    "helper_failed": (
        "The flashing step stopped unexpectedly. Try again; if it keeps happening, use "
        "\"Get help\"."
    ),
    "unsupported": "Flashing from this app isn't supported on this computer yet.",
}

#: code -> Action the UI offers as a button (none for the rest)
ACTIONS: dict[str, str] = {
    code: Action.RETRY
    for code in ("cancelled", "card_removed", "card_changed", "write_protected",
                 "access_denied", "card_in_use", "uac_cancelled", "verify_failed",
                 "write_failed", "sha_mismatch", "image_changed", "offline",
                 "download_failed", "disk_full", "helper_failed")
}


class FlashError(Exception):
    """A flashing failure with a stable ``code`` (keys of :data:`MESSAGES`)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def message_for_owner(self) -> str:
        return MESSAGES.get(self.code, MESSAGES["helper_failed"])

    def check(self) -> CheckResult:
        return CheckResult(False, f"flash_{self.code}", self.message_for_owner, self.detail,
                           ACTIONS.get(self.code))


# Windows error codes (winerror) and errno values worth translating
_WIN_CODES = {
    5: "access_denied",  # ERROR_ACCESS_DENIED
    19: "write_protected",  # ERROR_WRITE_PROTECT
    21: "card_removed",  # ERROR_NOT_READY
    2: "card_removed",  # ERROR_FILE_NOT_FOUND (drive gone)
    3: "card_removed",  # ERROR_PATH_NOT_FOUND
    31: "write_failed",  # ERROR_GEN_FAILURE
    32: "card_in_use",  # ERROR_SHARING_VIOLATION
    33: "card_in_use",  # ERROR_LOCK_VIOLATION
    55: "card_removed",  # ERROR_DEV_NOT_EXIST
    433: "card_removed",  # ERROR_NO_SUCH_DEVICE
    1117: "write_failed",  # ERROR_IO_DEVICE
    1167: "card_removed",  # ERROR_DEVICE_NOT_CONNECTED
    1110: "card_removed",  # ERROR_MEDIA_CHANGED
    1112: "card_removed",  # ERROR_NO_MEDIA_IN_DRIVE
    112: "too_small",  # ERROR_DISK_FULL
    27: "too_small",  # ERROR_SECTOR_NOT_FOUND (past the end of a small/fake card)
}
_ERRNO_CODES = {
    errno.EROFS: "write_protected",
    errno.EACCES: "access_denied",
    errno.EPERM: "access_denied",
    errno.EBUSY: "card_in_use",
    errno.ENODEV: "card_removed",
    errno.ENXIO: "card_removed",
    errno.ENOENT: "card_removed",
    errno.EIO: "write_failed",
    errno.ENOSPC: "too_small",
}
_ENOMEDIUM = getattr(errno, "ENOMEDIUM", 123)


def from_oserror(exc: OSError, default: str = "write_failed") -> FlashError:
    """Translate an OSError from the device into a FlashError with a plain-words code."""
    winerror = getattr(exc, "winerror", None)
    code = None
    if winerror is not None:
        code = _WIN_CODES.get(winerror)
    if code is None and exc.errno == _ENOMEDIUM:
        code = "card_removed"
    if code is None and exc.errno is not None:
        code = _ERRNO_CODES.get(exc.errno)
    return FlashError(code or default, f"{type(exc).__name__}: {exc}")
