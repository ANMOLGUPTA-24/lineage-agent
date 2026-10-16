"""reStructuredText -> plain text that can be quoted word for word next to a deep link."""
import re

MAX_TEXT = 900


def demarkup(text: str) -> str:
    text = re.sub(r":pep:`(?:[^`<]*<)?(\d+)(?:#[^`>]*)?>?`", r"PEP \1", text)
    text = re.sub(r":(?:py:)?[a-z]+:`~?([^`<]+?)(?:\s*<[^>]+>)?`", r"\1", text)
    text = re.sub(r"`([^`<]+?)\s*<[^>]+>`_{1,2}", r"\1", text)
    text = re.sub(r"(?<!`)`([^`]+)`_{1,2}", r"\1", text)                  # named / anonymous links
    text = re.sub(r"``([^`]+)``", r"`\1`", text)
    # Footnote references, with the "see" that pointed at them: "Rejected; see [1]_." -> "Rejected."
    text = re.sub(r"[;,]?\s*(?:\(\s*)?(?:see|cf\.)\s+(?:\[#?[\w-]*\]_[,\s]*)+\)?", "", text, flags=re.I)
    text = re.sub(r"\s?\[#?[\w-]*\]_", "", text)
    text = re.sub(r"^\s*\.\. .*$", "", text, flags=re.M)                  # directives
    text = re.sub(r"::\s*$", ":", text, flags=re.M)
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


def clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[: end + 1] if end > 200 else cut.rstrip() + "…"
