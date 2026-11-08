"""Normalisation prudente des noms, adaptée du script fourni par l'utilisateur."""
import unicodedata


def name_key(text: str) -> str:
    """Ignore casse/ponctuation/accents latins, conserve les autres écritures.

    Les largeurs japonaises sont harmonisées, mais カ et ガ restent distincts.
    Les mentions d'édition ne sont pas retirées.
    """
    text = unicodedata.normalize("NFKC", text.casefold())
    result, previous_latin = [], False
    for char in text:
        if char.isalnum():
            previous_latin = "LATIN" in unicodedata.name(char, "")
            if previous_latin:
                result.extend(c for c in unicodedata.normalize("NFKD", char) if c.isalnum())
            else:
                result.append(char)
        elif unicodedata.category(char).startswith("M") and not previous_latin:
            result.append(char)
    return "".join(result)
