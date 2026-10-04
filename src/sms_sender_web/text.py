"""Text people type, made Persian (spec 4.11): the Persian «ی» and «ک»,
never the Arabic «ي», «ى» or «ك» that some keyboards and Excel produce."""

_PERSIAN = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})


def persian_text(value: str) -> str:
    return value.translate(_PERSIAN)
