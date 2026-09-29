"""Download full texts of selected papers and convert them to Markdown.

Claude's WebFetch only returns a model-written summary of a page, so the main
research agent reads papers from local files instead. Order of attempts:

1. arXiv HTML (``arxiv.org/html/<id>``): keeps structure; LaTeX kept as ``$...$``
2. arXiv PDF
3. an explicit ``pdf_url``
4. open-access PDF via OpenAlex (for DOIs)
5. the landing page URL (HTML article text or PDF)
"""

from __future__ import annotations

import asyncio
import io
import json
import ipaddress
import logging
import re
import socket
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from app.models import RankedCandidate

log = logging.getLogger(__name__)

USER_AGENT = "paper-podcast/0.1 (personal research podcast generator)"
WRAP = 110


MAX_REDIRECTS = 5


class DownloadError(RuntimeError):
    pass


@dataclass
class Fetched:
    url: str
    content_type: str
    content: bytes
    encoding: str

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")


@dataclass
class PaperFile:
    id: str
    title: str
    file: str = ""
    source_url: str = ""
    format: str = ""
    lines: int = 0
    truncated: bool = False
    error: str = ""


# --- URL safety ---------------------------------------------------------------


def check_public_https(url: str) -> None:
    """Only public https URLs; the URLs come from web content, so no internal hosts."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DownloadError(f"only https URLs are allowed: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443)
    except socket.gaierror as exc:
        raise DownloadError(f"cannot resolve {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise DownloadError(f"refusing non-public address {ip} for {parsed.hostname}")


# --- Conversion -----------------------------------------------------------------


def _wrap(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return textwrap.fill(text, WRAP, break_long_words=False, break_on_hyphens=False) if text else ""


def _inline_text(node: Tag) -> str:
    parts = []
    for child in node.descendants:
        if isinstance(child, Tag) and child.name == "math":
            tex = child.get("alttext") or child.get_text(" ", strip=True)
            parts.append(f" ${tex}$ ")
        elif isinstance(child, NavigableString):
            if child.find_parent("math") is None and child.find_parent(["script", "style"]) is None:
                parts.append(str(child))
    return "".join(parts)


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "nav", "header", "footer", "noscript", "form", "button", "svg"]):
        junk.decompose()
    for junk in soup.select(".ltx_page_header, .ltx_page_footer, .ltx_bibliography, .ltx_authors .ltx_contact"):
        junk.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup

    blocks: list[str] = []
    seen: set[int] = set()
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "figcaption", "table", "blockquote"]):
        if any(id(parent) in seen for parent in node.parents):
            continue
        seen.add(id(node))
        if node.name.startswith("h"):
            text = re.sub(r"\s+", " ", _inline_text(node)).strip()
            if text:
                blocks.append("#" * int(node.name[1]) + " " + text)
        elif node.name == "table":
            rows = []
            for tr in node.find_all("tr"):
                cells = [re.sub(r"\s+", " ", _inline_text(c)).strip() for c in tr.find_all(["td", "th"])]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                blocks.append("\n".join(rows))
        else:
            text = _wrap(_inline_text(node))
            if not text:
                continue
            if node.name == "li":
                text = "- " + text.replace("\n", "\n  ")
            elif node.name == "figcaption":
                text = "*" + text + "*"
            elif node.name == "blockquote":
                text = "> " + text.replace("\n", "\n> ")
            blocks.append(text)
    return "\n\n".join(blocks)


def pdf_to_markdown(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        # Re-join hyphenated line breaks and rewrap paragraphs.
        text = re.sub(r"-\n(?=[a-zäöü])", "", text)
        paragraphs = [_wrap(p) for p in re.split(r"\n\s*\n", text)]
        pages.append(f"<!-- Seite {number} -->\n\n" + "\n\n".join(p for p in paragraphs if p))
    return "\n\n".join(pages)


# --- Downloading ----------------------------------------------------------------


def normalize_arxiv_id(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(arxiv:|https?://arxiv\.org/(abs|pdf|html)/)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.pdf$", "", value)
    return re.sub(r"v\d+$", "", value)


def file_key(paper_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", paper_id).strip("_")[:80]


class PaperDownloader:
    def __init__(
        self,
        client: httpx.AsyncClient,
        max_bytes: int,
        max_chars: int,
        check_urls: bool = True,
    ):
        self.client = client
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.check_urls = check_urls

    async def _get(self, url: str) -> Fetched:
        """GET with size limit; every redirect hop is checked before it is requested."""
        for _ in range(MAX_REDIRECTS + 1):
            if self.check_urls:
                await asyncio.to_thread(check_public_https, url)
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code != 200:
                    raise DownloadError(f"HTTP {response.status_code} for {url}")
                length = int(response.headers.get("content-length") or 0)
                if length > self.max_bytes:
                    raise DownloadError(f"too large ({length} bytes): {url}")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise DownloadError(f"too large (> {self.max_bytes} bytes): {url}")
                    chunks.append(chunk)
                return Fetched(url, response.headers.get("content-type", ""), b"".join(chunks),
                               response.encoding or "utf-8")
        raise DownloadError(f"too many redirects: {url}")

    async def _fetch_document(self, url: str) -> tuple[str, str, str]:
        fetched = await self._get(url)
        if "pdf" in fetched.content_type or fetched.content[:5] == b"%PDF-":
            return pdf_to_markdown(fetched.content), "pdf", fetched.url
        if "html" in fetched.content_type or "xml" in fetched.content_type or not fetched.content_type:
            return html_to_markdown(fetched.text), "html", fetched.url
        raise DownloadError(f"unsupported content type {fetched.content_type!r}: {url}")

    async def _openalex_pdf(self, doi: str) -> str:
        fetched = await self._get(f"https://api.openalex.org/works/doi:{doi}")
        data = json.loads(fetched.text)
        for location in [data.get("best_oa_location") or {}, *(data.get("locations") or [])]:
            if location.get("pdf_url"):
                return location["pdf_url"]
        raise DownloadError(f"no open-access PDF for DOI {doi}")

    def _attempts(self, candidate: RankedCandidate):
        if candidate.arxiv_id:
            arxiv = normalize_arxiv_id(candidate.arxiv_id)
            yield f"https://arxiv.org/html/{arxiv}", None
            yield f"https://arxiv.org/pdf/{arxiv}", None
        if candidate.pdf_url:
            yield candidate.pdf_url, None
        if candidate.doi:
            yield None, candidate.doi
        if candidate.url:
            yield candidate.url, None

    async def download(self, candidate: RankedCandidate, out_dir: Path) -> PaperFile:
        paper = PaperFile(id=candidate.id, title=candidate.title)
        errors = []
        for url, doi in self._attempts(candidate):
            try:
                if doi:
                    url = await self._openalex_pdf(doi)
                text, fmt, url = await self._fetch_document(url)
                if len(text) < 2000:  # abstract-only pages, paywalls, cookie walls
                    raise DownloadError(f"too little text ({len(text)} chars): {url}")
            except (DownloadError, httpx.HTTPError, ValueError) as exc:
                errors.append(f"{url or doi}: {exc}")
                continue
            if len(text) > self.max_chars:
                text = text[: self.max_chars] + "\n\n[… gekürzt …]"
                paper.truncated = True
            header = f"# {candidate.title}\n\nQuelle: {url}\n\n"
            path = out_dir / f"{file_key(candidate.id)}.md"
            path.write_text(header + text + "\n", encoding="utf-8")
            paper.file = f"papers/{path.name}"
            paper.source_url = url
            paper.format = fmt
            paper.lines = (header + text).count("\n") + 1
            return paper
        paper.error = "; ".join(errors) or "no URL to try"
        return paper


async def download_all(
    candidates: list[RankedCandidate],
    out_dir: Path,
    max_bytes: int,
    max_chars: int,
    timeout_s: int,
    client: httpx.AsyncClient | None = None,
    check_urls: bool = True,
) -> list[PaperFile]:
    out_dir.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": USER_AGENT})
    try:
        downloader = PaperDownloader(client, max_bytes, max_chars, check_urls)
        semaphore = asyncio.Semaphore(3)

        async def one(candidate):
            async with semaphore:
                return await downloader.download(candidate, out_dir)

        return list(await asyncio.gather(*(one(c) for c in candidates)))
    finally:
        if own_client:
            await client.aclose()


def to_index(papers: list[PaperFile]) -> list[dict]:
    return [asdict(p) for p in papers]
