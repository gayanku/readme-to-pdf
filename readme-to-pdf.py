#!/usr/bin/env python3
"""
GitHub README → PDF Converter
==============================
Fetches a repository's README.md (including images and tables)
and renders it to a PDF that mirrors the GitHub layout.

Usage
-----
  python readme_to_pdf.py https://github.com/owner/repo
  python readme_to_pdf.py https://github.com/owner/repo -o output.pdf
  python readme_to_pdf.py https://github.com/owner/repo --token ghp_xxxx

A GitHub token is optional for public repos but lifts the API rate limit
(60 → 5 000 req/hr) and is required for private repositories.
Set it via  --token  or the GITHUB_TOKEN environment variable.

Dependencies
------------ 
  pip install requests markdown Pygments playwright
  playwright install chromium
  
Playwright spins up a headless Chromium, so image/table rendering is pixel-perfect and there are no native library dependencies to wrestle with on Windows.

Author(s)
Gayan Kulatilleke & Claude Sonnet 4.6
"""

import argparse
import os
import re
import sys
from urllib.parse import urlparse

import requests
import markdown

from playwright.sync_api import sync_playwright


# ──────────────────────────────────────────────────────────────────────────────
# GitHub helpers
# ──────────────────────────────────────────────────────────────────────────────

README_NAMES = ["README.md", "readme.md", "Readme.md", "README.rst", "README"]


def parse_github_url(url: str) -> tuple[str, str]:
    """Parse a GitHub URL and return (owner, repo)."""
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "github.com":
        raise ValueError(f"Expected a github.com URL, got: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("URL must include owner and repo — e.g. https://github.com/owner/repo")
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo


def _api_headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_readme_raw(owner: str, repo: str, branch: str = "HEAD") -> tuple[str, str]:
    """
    Fetch README content directly from raw.githubusercontent.com.
    Tries common filenames and returns (content, filename).
    This approach never touches the GitHub REST API, so it isn't rate-limited.
    """
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
    for name in README_NAMES:
        resp = requests.get(f"{base}/{name}", timeout=30)
        if resp.status_code == 200:
            return resp.text, name
    raise SystemExit(
        f"Error: no README found in '{owner}/{repo}' on branch '{branch}'.\n"
        "Try --branch <name> if the default branch name differs."
    )


def fetch_readme_api(owner: str, repo: str, token: str) -> tuple[str, str, str]:
    """
    Fetch README via the GitHub REST API (requires a token for private repos or
    when the unauthenticated rate limit is exhausted).
    Returns (content, filename, branch).
    """
    # Get default branch
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers=_api_headers(token), timeout=30,
    )
    if r.status_code == 404:
        raise SystemExit(f"Error: repo '{owner}/{repo}' not found (or private — use --token).")
    r.raise_for_status()
    branch = r.json()["default_branch"]

    # Get README metadata
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/readme",
        headers=_api_headers(token), timeout=30,
    )
    if r.status_code == 404:
        raise SystemExit(f"Error: no README found in '{owner}/{repo}'.")
    r.raise_for_status()
    meta = r.json()

    content = requests.get(meta["download_url"], timeout=30)
    content.raise_for_status()
    return content.text, meta.get("name", "README.md"), branch


def fetch_readme(
    owner: str, repo: str, branch: str | None, token: str | None
) -> tuple[str, str, str]:
    """
    Smart fetch: uses raw URLs when possible (no rate limit), falls back to
    the API when a token is provided.
    Returns (content, filename, resolved_branch).
    """
    if token:
        # Token supplied → use the API so private repos work too
        text, name, resolved = fetch_readme_api(owner, repo, token)
        if branch and branch != resolved:
            # User asked for a specific branch; re-fetch raw on that branch
            text, name = fetch_readme_raw(owner, repo, branch)
            resolved = branch
        return text, name, resolved

    # No token → raw URL (works for all public repos, zero API calls)
    resolved = branch or "HEAD"
    text, name = fetch_readme_raw(owner, repo, resolved)
    return text, name, resolved


# ──────────────────────────────────────────────────────────────────────────────
# Image URL resolver
# ──────────────────────────────────────────────────────────────────────────────

def resolve_image_urls(html: str, owner: str, repo: str, branch: str) -> str:
    """
    Rewrite relative ``src`` attributes to absolute raw.githubusercontent.com URLs
    so WeasyPrint can fetch them.  Absolute and data: URLs are left untouched.
    """
    raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"

    def _fix(m: re.Match) -> str:
        quote, src = m.group(1), m.group(2)
        if src.startswith(("http://", "https://", "data:", "//")):
            return m.group(0)                # already absolute → keep
        src = re.sub(r"^\.?/+", "", src)     # strip leading ./ or /
        return f"src={quote}{raw_base}{src}{quote}"

    return re.sub(r'src=(["\'])([^"\']*)\1', _fix, html)


# ──────────────────────────────────────────────────────────────────────────────
# CSS / styling
# ──────────────────────────────────────────────────────────────────────────────

_BODY_CSS = """
* { box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
                 Arial, sans-serif;
    font-size: 16px;
    line-height: 1.6;
    color: #24292f;
    background: #fff;
    padding: 32px 40px;
    max-width: 900px;
    margin: 0 auto;
}

/* Headings */
h1, h2, h3, h4, h5, h6 {
    margin-top: 24px; margin-bottom: 16px;
    font-weight: 600; line-height: 1.25;
}
h1 { font-size: 2em;     border-bottom: 1px solid #d0d7de; padding-bottom: .3em; }
h2 { font-size: 1.5em;   border-bottom: 1px solid #d0d7de; padding-bottom: .3em; }
h3 { font-size: 1.25em; }
h4 { font-size: 1em;    }
h5 { font-size: .875em; }
h6 { font-size: .85em;  color: #57606a; }

/* Body text */
p  { margin-top: 0; margin-bottom: 16px; }
a  { color: #0969da; text-decoration: none; }

/* Lists */
ul, ol { margin-bottom: 16px; padding-left: 2em; }
li     { margin-bottom: 4px; }
li > ul, li > ol { margin-top: 4px; margin-bottom: 0; }

/* Blockquote */
blockquote {
    margin: 0 0 16px; padding: 0 1em;
    color: #57606a; border-left: .25em solid #d0d7de;
}

/* Inline code */
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 85%;
    padding: .2em .4em;
    background: rgba(175,184,193,.2);
    border-radius: 6px;
}

/* Code blocks */
pre {
    margin-bottom: 16px; padding: 16px;
    background: #f6f8fa; border-radius: 6px;
    font-size: 85%; line-height: 1.45; overflow-x: auto;
}
pre code { background: none; padding: 0; font-size: 100%; }

/* Syntax-highlighted blocks (Pygments via codehilite) */
.codehilite      { background: #f6f8fa; border-radius: 6px;
                   padding: 16px; margin-bottom: 16px; }
.codehilite pre  { background: none; padding: 0; margin: 0; }
.codehilite code { background: none; padding: 0; }

/* Tables */
table {
    border-collapse: collapse; width: 100%;
    margin-bottom: 16px; display: table; font-size: .95em;
}
th, td { padding: 6px 13px; border: 1px solid #d0d7de; }
th     { font-weight: 600; background: #f6f8fa; }
tr:nth-child(even) { background: #f6f8fa; }

/* Images */
img { max-width: 100%; height: auto; display: block; margin: 16px auto; }
/* Inline badges / small images */
p img, li img { display: inline; margin: 0 2px; vertical-align: middle; }

hr { border: 0; border-top: 1px solid #d0d7de; margin: 24px 0; }
"""

_PAGE_CSS = """
@page {
    size: Letter;
    margin: 0.75in 0.75in 1in 0.75in;   /* extra bottom margin for footer */
    @bottom-center {
        content: "Created with GitHub README to PDF Converter  •  github.com/gayanku/readme-to-pdf";
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        font-size: 9px;
        color: #8c959f;
    }
}
"""

# Playwright footer — same text, styled via inline HTML
_PLAYWRIGHT_FOOTER = """
<div style="
    width: 100%;
    text-align: center;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 9px;
    color: #8c959f;
    padding: 0 0.75in;
">
  Created with GitHub README to PDF Converter &nbsp;•&nbsp;
  github.com/gayanku/readme-to-pdf
</div>
"""

_MD_EXTENSIONS = [
    "markdown.extensions.tables",
    "markdown.extensions.fenced_code",
    "markdown.extensions.codehilite",
    "markdown.extensions.toc",
    "markdown.extensions.nl2br",
    "markdown.extensions.sane_lists",
    "markdown.extensions.attr_list",
    "markdown.extensions.def_list",
    "markdown.extensions.footnotes",
]

_MD_EXT_CONFIG = {
    "markdown.extensions.codehilite": {
        "guess_lang": False,
        "use_pygments": True,
    }
}


# ──────────────────────────────────────────────────────────────────────────────
# Core conversion
# ──────────────────────────────────────────────────────────────────────────────

def build_html(md_text: str, owner: str, repo: str, branch: str) -> str:
    """Convert Markdown to a self-contained HTML document string."""
    md_parser = markdown.Markdown(
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXT_CONFIG,
    )
    body = md_parser.convert(md_text)
    body = resolve_image_urls(body, owner, repo, branch)

    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        f'  <title>README — {owner}/{repo}</title>\n'
        f'  <style>{_BODY_CSS}</style>\n'
        '</head>\n'
        '<body>\n'
        f'{body}\n'
        '</body>\n'
        '</html>'
    )

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright (Chromium) — works on Windows with no extra deps.
            pip install playwright
            playwright install chromium
    """
    # from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")   # waits for images to load
        # page.pdf(
        #    path=output_path,
        #    format="Letter",
        #    margin={"top": "0.75in", "bottom": "0.75in",
        #            "left": "0.75in", "right": "0.75in"},
        #    print_background=True,
        #)
        
        page.pdf(
                path=output_path,
                format="Letter",
                margin={
                    "top": "0.75in",
                    "bottom": "1in",    # extra space for footer
                    "left": "0.75in",
                    "right": "0.75in",
                },
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",   # empty header
                footer_template=_PLAYWRIGHT_FOOTER,
        )
        browser.close()
        
# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="readme_to_pdf",
        description="Convert a GitHub repository's README.md to a PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("repo_url", help="GitHub repository URL")
    parser.add_argument(
        "-o", "--output", metavar="FILE",
        help="Output PDF path (default: <repo>_README.pdf)",
    )
    parser.add_argument(
        "-t", "--token", metavar="TOKEN",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub personal access token (or set GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "-b", "--branch", metavar="BRANCH",
        help="Branch to read from (default: repo's default branch)",
    )
    args = parser.parse_args()

    # 1 ── Parse URL ──────────────────────────────────────────────────────────
    print(f"📦  Repository : {args.repo_url}")
    try:
        owner, repo = parse_github_url(args.repo_url)
    except ValueError as exc:
        sys.exit(f"Error: {exc}")
    print(f"             → {owner}/{repo}")

    # 2 ── Fetch README ───────────────────────────────────────────────────────
    print("📄  Fetching README …")
    readme_text, readme_name, branch = fetch_readme(
        owner, repo, args.branch, args.token
    )
    print(f"             → {readme_name}  (branch: {branch},  {len(readme_text):,} chars)")

    # 3 ── Convert Markdown → HTML ────────────────────────────────────────────
    print("🔄  Converting Markdown → HTML …")
    html = build_html(readme_text, owner, repo, branch)

    # 4 ── Render PDF ─────────────────────────────────────────────────────────
    output_path = args.output or f"{repo}_README.pdf"
    print(f"🖨   Rendering PDF …")
    try:
        render_pdf(html, output_path)
    except Exception as exc:
        sys.exit(f"PDF rendering failed: {exc}")

    size_kb = os.path.getsize(output_path) / 1024
    print(f"✅  Saved → '{output_path}'  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
