"""Documents Kepler can read from his own folders: his downloads and his file server.

Both are mounted read-only and Kepler never writes, moves or deletes anything in them. She reads a
document only when he asks for it, by name; nothing is indexed or read on its own. Paths are
checked after resolving links, so nothing outside those folders can be reached.
"""
import os
import zipfile
from xml.etree import ElementTree

from .research import KeplerError

KINDS = {".pdf": "PDF", ".docx": "Word document", ".txt": "text file", ".md": "text file"}
MAX_WALK = 50_000                  # files looked at when searching by name
MAX_TEXT_BYTES = 2_000_000         # a text file's first 2 MB
MAX_DOCX_XML = 50_000_000          # a Word file's text part, uncompressed (zip bombs stop here)
MAX_PDF_PAGES = 200
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _inside(root: str, path: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def find(name: str, roots: list[tuple[str, str]]) -> tuple[str, str, str]:
    """(path, place, relative path) for the document he named. `roots` is [(place, folder)].
    A name can be a path inside a folder ("school/notes.pdf"), a file name ("notes.pdf"), or words
    from it ("azure notes"). More than one match → he's asked which."""
    name = name.strip().strip("/")
    if not name:
        raise KeplerError("Which document? Give me its name.")
    ext = os.path.splitext(name)[1].lower()
    if ext and ext not in KINDS and " " not in name:
        raise KeplerError("I can read PDFs, Word documents (.docx) and text files.")
    roots = [(place, os.path.realpath(folder)) for place, folder in roots if folder and os.path.isdir(folder)]
    places = " or ".join(p for p, _ in roots) or "your folders"
    # A path (or exact name) inside one of the folders.
    for place, root in roots:
        path = os.path.realpath(os.path.join(root, name))
        if not _inside(root, path):
            raise KeplerError(f"I only read documents inside {places}.")
        if os.path.isfile(path):
            if os.path.splitext(path)[1].lower() not in KINDS:
                raise KeplerError("I can read PDFs, Word documents (.docx) and text files.")
            return path, place, os.path.relpath(path, root)
    if os.sep in name or ".." in name.split(os.sep):
        raise KeplerError(f"There's no {name} in {places}.")
    # Otherwise search by name: an exact file name wins, then files whose name has all his words.
    wanted = name.lower()
    words = [w for w in os.path.splitext(wanted)[0].replace("_", " ").replace("-", " ").split() if w]
    exact, close, seen = [], [], 0
    for place, root in roots:
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                seen += 1
                if seen > MAX_WALK:
                    break
                if f.startswith(".") or os.path.splitext(f)[1].lower() not in KINDS:
                    continue
                path = os.path.realpath(os.path.join(folder, f))
                if not _inside(root, path) or not os.path.isfile(path):
                    continue                              # a link pointing outside: never followed
                hit = (path, place, os.path.relpath(path, root))
                low = f.lower()
                if low == wanted or os.path.splitext(low)[0] == wanted:
                    exact.append(hit)
                elif words and all(w in low.replace("_", " ").replace("-", " ") for w in words):
                    close.append(hit)
    for hits in (exact, close):
        if len(hits) == 1:
            return hits[0]
        if hits:
            shown = ", ".join(rel for _, _, rel in hits[:5]) + (f" and {len(hits) - 5} more" if len(hits) > 5 else "")
            raise KeplerError(f"I found more than one: {shown}. Which one?")
    raise KeplerError(f"There's no {name} in {places}.")


def read(path: str) -> tuple[str, int | None]:
    """The document's text and, for a PDF, its page count."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        text = "\n\n".join((p.extract_text() or "").strip() for p in reader.pages[:MAX_PDF_PAGES]).strip()
        if not text:
            raise KeplerError("That PDF is only images (scanned), so there's no text to read.")
        return text, len(reader.pages)
    if ext == ".docx":
        return _docx(path), None
    with open(path, "rb") as f:
        return f.read(MAX_TEXT_BYTES).decode("utf-8", errors="replace").strip(), None


def _docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML:
                raise KeplerError("That Word document is too large to read.")
            root = ElementTree.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise KeplerError("That doesn't look like a Word document I can read (only .docx, not old .doc).")
    lines = []
    body = root.find(f"{W}body")
    for block in (body if body is not None else []):
        if block.tag == f"{W}p":
            lines.append(_para(block))
        elif block.tag == f"{W}tbl":
            for row in block.iter(f"{W}tr"):
                cells = [" ".join(_para(p) for p in c.iter(f"{W}p")).strip() for c in row.iter(f"{W}tc")]
                lines.append(" | ".join(cells))
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    text = text.strip()
    if not text:
        raise KeplerError("That Word document has no text in it.")
    return text


def _para(p) -> str:
    out = []
    for el in p.iter():
        if el.tag == f"{W}t" and el.text:
            out.append(el.text)
        elif el.tag == f"{W}tab":
            out.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            out.append("\n")
    return "".join(out)
