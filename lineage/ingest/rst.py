"""reStructuredText -> plain text that can be quoted word for word next to a deep link."""
import re
from pathlib import Path

MAX_TEXT = 900
UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")


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


def sections(path: Path) -> list[dict]:
    """Flat list of {level, title, body} using rst's rule: heading levels follow the order in
    which underline characters first appear."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    levels: list[str] = []
    out, cur = [], None
    i = 0
    while i < len(lines):
        if (i + 1 < len(lines) and lines[i].strip() and not lines[i].startswith(" ")
                and UNDERLINE.match(lines[i + 1]) and len(lines[i + 1].rstrip()) >= len(lines[i].rstrip())):
            ch = lines[i + 1][0]
            if ch not in levels:
                levels.append(ch)
            cur = {"level": levels.index(ch), "title": lines[i].strip(), "body": []}
            out.append(cur)
            i += 2
            continue
        if cur is not None:
            cur["body"].append(lines[i])
        i += 1
    for s in out:
        s["body"] = "\n".join(s["body"])
    return out
