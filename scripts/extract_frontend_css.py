#!/usr/bin/env python3
"""Extract inline <style> blocks from frontend HTML into external CSS files."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

STYLE_RE = re.compile(r"<style>\s*(.*?)\s*</style>", re.DOTALL | re.IGNORECASE)

# (html path relative to FRONTEND, css path relative to FRONTEND, link href from html dir)
HTML_CSS_MAP: list[tuple[str, str, str]] = [
    ("login.html", "css/redwood-auth.css", "/css/redwood-auth.css"),
    ("register.html", "css/redwood-auth.css", "/css/redwood-auth.css"),
    ("login-admin.html", "css/redwood-auth.css", "/css/redwood-auth.css"),
    ("compte-desactive.html", "css/redwood-auth.css", "/css/redwood-auth.css"),
    ("maintenance.html", "css/redwood-static.css", "/css/redwood-static.css"),
    ("watch/index.html", "css/redwood-watch-home.css", "/css/redwood-watch-home.css"),
    ("watch/films.html", "css/redwood-watch-list.css", "/css/redwood-watch-list.css"),
    ("watch/categories.html", "css/redwood-watch-categories.css", "/css/redwood-watch-categories.css"),
    ("watch/pick.html", "css/redwood-watch-pick.css", "/css/redwood-watch-pick.css"),
    ("watch/serie.html", "css/redwood-watch-serie.css", "/css/redwood-watch-serie.css"),
    ("watch/film.html", "css/redwood-watch-film.css", "/css/redwood-watch-film.css"),
    ("watch/settings.html", "css/redwood-watch-account.css", "/css/redwood-watch-account.css"),
    ("watch/support.html", "css/redwood-watch-support.css", "/css/redwood-watch-support.css"),
    ("watch/invitations.html", "css/redwood-watch-invitations.css", "/css/redwood-watch-invitations.css"),
    ("admin/index.html", "admin/admin.css", "/admin/admin.css"),
]

WATCH_SHARED_LINKS = """  <link rel="stylesheet" href="/css/redwood-watch-shell.css" />
  <link rel="stylesheet" href="/css/redwood-watch-ui.css" />"""

def extract_style(html: str) -> str | None:
    m = STYLE_RE.search(html)
    return m.group(1).strip() if m else None


def replace_style_with_links(html: str, links_block: str) -> str:
    return STYLE_RE.sub(links_block, html, count=1)


def ensure_watch_shell_from(css_text: str) -> str:
    """Build shell CSS: tokens + reset + nav + brand + footer from any watch page block."""
    lines = []
    for line in css_text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Stop before page-specific selectors
        if s.startswith(
            (
                ".hero",
                ".row-head",
                ".row-scroll",
                ".row-carousel",
                ".home-search",
                ".donation",
                "main{",
                "main ",
                "h1{",
                ".lead",
                ".grid",
                ".wrap",
                ".backdrop",
                ".card{",
                ".card ",
                ".card.",
                ".card:",
                ".badge-new",
                "section",
                ".btn",
                ".msg",
                ".hint",
                ".identity",
                ".player",
                ".plyr",
                ".similar",
                ".c{",
                ".c ",
                ".empty",
                ".meta",
                ".syno",
                ".trailers",
                ".trailer",
                ".film-ad",
                ".video-loader",
                ".spinner",
                "video{",
                ".row{",
                ".pill",
                ".series-bar",
                ".poster",
                ".title",
                ".user-score",
                ".hero-layout",
                ".hero-body",
                ".inv-",
                ".ticket",
                ".support",
            )
        ):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> None:
    written: dict[str, str] = {}

    for html_rel, css_rel, link_href in HTML_CSS_MAP:
        html_path = FRONTEND / html_rel
        if not html_path.exists():
            print(f"skip missing {html_rel}")
            continue
        html = html_path.read_text(encoding="utf-8")
        css = extract_style(html)
        if not css:
            print(f"no style in {html_rel}")
            continue

        css_path = FRONTEND / css_rel
        if css_rel not in written:
            css_path.parent.mkdir(parents=True, exist_ok=True)
            css_path.write_text(css + "\n", encoding="utf-8")
            written[css_rel] = css
            print(f"wrote {css_rel} from {html_rel}")
        elif written[css_rel] != css and html_rel in (
            "watch/categories.html",
            "watch/pick.html",
        ):
            print(f"note: {html_rel} css differs from {css_rel} source — using first extract")

        if html_rel.startswith("watch/"):
            links = WATCH_SHARED_LINKS + f'\n  <link rel="stylesheet" href="{link_href}" />'
        else:
            links = f'  <link rel="stylesheet" href="{link_href}" />'

        new_html = replace_style_with_links(html, links)
        html_path.write_text(new_html, encoding="utf-8")
        print(f"patched {html_rel}")

    # Build watch shell + ui from index + app.js snippets
    home_path = FRONTEND / "css/redwood-watch-home.css"
    if home_path.exists():
        home_css = home_path.read_text(encoding="utf-8")
        shell = ensure_watch_shell_from(home_css)
        shell_path = FRONTEND / "css/redwood-watch-shell.css"
        shell_path.write_text(
            "/* Shared watch shell — nav, brand, footer */\n" + shell + "\n",
            encoding="utf-8",
        )
        print("wrote css/redwood-watch-shell.css")

    app_js = FRONTEND / "watch/app.js"
    if app_js.exists():
        ui_parts: list[str] = []
        for fn in (
            "injectWatchMobileNavStyles",
            "injectWatchNavUserStyles",
            "injectWatchAnnouncementStyles",
            "injectWatchLoadingStyles",
        ):
            block = re.search(
                rf"function {fn}\(\).*?style\.textContent = `\s*(.*?)\s*`;",
                app_js.read_text(encoding="utf-8"),
                re.DOTALL,
            )
            if block:
                ui_parts.append(f"/* from app.js {fn} */\n" + block.group(1).strip())
        ui_path = FRONTEND / "css/redwood-watch-ui.css"
        ui_path.write_text("\n\n".join(ui_parts) + "\n", encoding="utf-8")
        print("wrote css/redwood-watch-ui.css")


if __name__ == "__main__":
    main()
