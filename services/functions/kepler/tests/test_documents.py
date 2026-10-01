"""Reading his own documents: finding them by name in his downloads and file server, staying inside
those folders, and reading PDFs, Word files and text."""
import os
import zipfile

import pytest

from app import documents
from app.research import KeplerError


def pdf(path, text):
    """A real one-page PDF with a line of text (no library needed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def docx(path, paragraphs, table=None):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>"
                       for r in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')


@pytest.fixture
def folders(tmp_path):
    dl, fs = tmp_path / "downloads", tmp_path / "files"
    (fs / "School" / "AZ-104").mkdir(parents=True)
    (fs / "Media").mkdir()
    dl.mkdir()
    pdf(dl / "guide.pdf", "Downloaded guide")
    pdf(fs / "School" / "AZ-104" / "Azure Networking Notes.pdf", "Virtual networks and subnets")
    pdf(fs / "School" / "AZ-104" / "Azure Storage Notes.pdf", "Blob storage tiers")
    docx(fs / "Resume_2026.docx", ["Kentrell", "IT Support"], table=[["Skill", "Years"], ["Azure", "2"]])
    (fs / "todo.md").write_text("# Todo\n- study\n")
    (fs / "Media" / "movie.mkv").write_bytes(b"\0" * 10)
    (tmp_path / "secret.pdf").write_bytes(b"%PDF")
    os.symlink(tmp_path / "secret.pdf", fs / "link.pdf")              # points outside: never read
    return [("your downloads", str(dl)), ("your file server", str(fs))]


def test_finds_by_path_name_or_words(folders):
    path, place, rel = documents.find("School/AZ-104/Azure Storage Notes.pdf", folders)
    assert place == "your file server" and rel == "School/AZ-104/Azure Storage Notes.pdf"
    assert documents.find("guide.pdf", folders)[1] == "your downloads"
    assert documents.find("resume_2026", folders)[2] == "Resume_2026.docx"
    assert documents.find("azure networking", folders)[2].endswith("Azure Networking Notes.pdf")
    assert documents.find("resume", folders)[2] == "Resume_2026.docx"


def test_asks_which_one_when_several_match(folders):
    with pytest.raises(KeplerError, match="more than one: School/AZ-104/Azure Networking Notes.pdf, "
                                          "School/AZ-104/Azure Storage Notes.pdf. Which one"):
        documents.find("azure notes", folders)


def test_stays_inside_his_folders(folders):
    with pytest.raises(KeplerError, match="only read documents inside your downloads or your file server"):
        documents.find("../secret.pdf", folders)
    with pytest.raises(KeplerError, match="only read documents inside"):
        documents.find("link.pdf", folders)                      # the link leads outside
    with pytest.raises(KeplerError, match="no secret"):
        documents.find("secret", folders)                        # a search never follows it either
    with pytest.raises(KeplerError, match="PDFs, Word documents"):
        documents.find("movie.mkv", folders)
    with pytest.raises(KeplerError, match="no movie"):
        documents.find("movie", folders)                         # media isn't a document
    with pytest.raises(KeplerError, match="no taxes in your downloads or your file server"):
        documents.find("taxes", folders)


def test_a_missing_file_server_is_just_skipped(folders):
    only = [folders[0], ("your file server", "/nonexistent/files")]
    assert documents.find("guide", only)[1] == "your downloads"
    with pytest.raises(KeplerError, match="no resume in your downloads\\."):
        documents.find("resume", only)


def test_reads_pdf_word_and_text(folders):
    text, pages = documents.read(documents.find("azure networking", folders)[0])
    assert "Virtual networks and subnets" in text and pages == 1
    text, pages = documents.read(documents.find("resume", folders)[0])
    assert text.splitlines()[:2] == ["Kentrell", "IT Support"] and "Azure | 2" in text and pages is None
    assert documents.read(documents.find("todo.md", folders)[0])[0].startswith("# Todo")


def test_old_or_broken_word_files_are_explained(tmp_path):
    bad = tmp_path / "old.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(KeplerError, match="not old .doc"):
        documents.read(str(bad))
