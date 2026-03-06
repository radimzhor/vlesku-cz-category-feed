import csv
import re
import unicodedata
from typing import List, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

SITEMAP_URL = "https://www.vlesku.cz/sitemap-cz.xml"
OUTPUT_CSV = "vlesku_category_feed.csv"
BASE_URL = "https://www.vlesku.cz"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; category-feed/1.0)"
}


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_sitemap(xml_text: str) -> List[str]:
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    urls = []

    if root.tag.endswith("sitemapindex"):
        for loc in root.findall(".//sm:loc", ns):
            if loc.text:
                sub_xml = fetch(loc.text.strip())
                urls.extend(parse_sitemap(sub_xml))
        return urls

    for url_el in root.findall(".//sm:url", ns):
        loc_el = url_el.find("sm:loc", ns)
        if loc_el is not None and loc_el.text:
            urls.append(loc_el.text.strip())

    return urls


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "home"
    return path.split("/")[-1]


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def stable_identity(name: str, url: str) -> str:
    handle = slugify(name)
    if handle:
        return handle
    return slug_from_url(url)


def get_h1(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def get_og_image(soup: BeautifulSoup) -> str:
    tag = soup.find("meta", attrs={"property": "og:image"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


def is_ignored_image(url: str) -> bool:
    if not url:
        return True

    ignored_patterns = [
        "/img/logo-cz.png",
        "logo-cz.png",
        "logo",
        "icon",
        "facebook",
        "instagram",
    ]
    lowered = url.lower()
    return any(pattern in lowered for pattern in ignored_patterns)


def absolutize_url(url: str, base: str = BASE_URL) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return base.rstrip("/") + "/" + url.lstrip("/")


def extract_first_product_image(soup: BeautifulSoup) -> str:
    selectors = [
        "#vypis li img",
        "#vypis a img",
        "ul#vypis img",
        "#vypis source",
    ]

    for selector in selectors:
        for el in soup.select(selector):
            candidates = []
            if el.name == "source":
                candidates.extend([
                    el.get("srcset"),
                    el.get("data-srcset"),
                ])
            else:
                candidates.extend([
                    el.get("src"),
                    el.get("data-src"),
                    el.get("data-original"),
                    el.get("data-lazy"),
                    el.get("srcset"),
                    el.get("data-srcset"),
                ])

            for candidate in candidates:
                if not candidate:
                    continue
                first_url = candidate.split(",")[0].strip().split(" ")[0].strip()
                full_url = absolutize_url(first_url)
                if full_url and not is_ignored_image(full_url):
                    return full_url

    return ""


def extract_breadcrumb(soup: BeautifulSoup) -> List[str]:
    marker = soup.find(string=lambda s: s and "Nacházíte se zde" in s)
    if not marker:
        return []

    parent = marker.parent if isinstance(marker.parent, Tag) else None
    if not parent:
        return []

    candidates = [parent]
    if isinstance(parent.parent, Tag):
        candidates.append(parent.parent)

    best = []
    for node in candidates:
        links = [a.get_text(" ", strip=True) for a in node.find_all("a")]
        node_text = node.get_text(" ", strip=True)

        cleaned = re.sub(r"^.*?Nacházíte se zde:\s*", "", node_text).strip()

        last_text = cleaned
        for link_text in links:
            last_text = last_text.replace(link_text, " ")
        last_text = re.sub(r"\s+", " ", last_text).strip(" >|")

        parts = [x for x in links if x]
        if last_text:
            parts.append(last_text)

        if len(parts) > len(best):
            best = parts

    return best


def clean_hierarchy(parts: List[str]) -> List[str]:
    cleaned = [p.strip() for p in parts if p and p.strip()]

    drop = {"úvod", "produkty"}
    while cleaned and cleaned[0].lower() in drop:
        cleaned.pop(0)

    return cleaned


def looks_like_product_page(soup: BeautifulSoup, url: str) -> bool:
    text = soup.get_text(" ", strip=True).lower()
    breadcrumb = extract_breadcrumb(soup)

    product_signals = [
        "vložit do košíku",
        "více o produktu",
        "hodnocení",
        "skladem",
    ]
    category_signals = [
        "kategorie",
        "řazení",
        "výrobce",
    ]

    product_score = sum(1 for s in product_signals if s in text)
    category_score = sum(1 for s in category_signals if s in text)

    if product_score >= 3 and category_score == 0:
        return True

    if product_score >= 3 and len(breadcrumb) <= 1:
        return True

    return False


def extract_image_fallback(soup: BeautifulSoup) -> str:
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        alt = (img.get("alt") or "").strip().lower()
        if not src:
            continue
        full_url = absolutize_url(src)
        if is_ignored_image(full_url):
            continue
        if any(x in alt for x in ["logo", "facebook", "instagram"]):
            continue
        return full_url
    return ""


def is_candidate_category_url(url: str) -> bool:
    parsed = urlparse(url)

    if parsed.query:
        return False

    bad_prefixes = [
        "/kontakt",
        "/pribeh",
        "/obchodni-podminky",
        "/vse-o-nakupu",
        "/prihlaseni",
        "/registrace",
    ]
    if parsed.path in ("", "/"):
        return False
    if any(parsed.path.startswith(x) for x in bad_prefixes):
        return False

    return True


def scrape_category(url: str) -> Optional[dict]:
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    if looks_like_product_page(soup, url):
        return None

    name = get_h1(soup)
    if not name:
        return None

    hierarchy_parts = clean_hierarchy(extract_breadcrumb(soup))
    if len(hierarchy_parts) < 1:
        return None

    hierarchy = " | ".join(hierarchy_parts)
    excluded_hierarchy_terms = [
        "Vše o nákupu",
        "Příběh",
        "Detailing blog",
        "Služby",
    ]
    if any(term in hierarchy for term in excluded_hierarchy_terms):
        return None

    og_image = absolutize_url(get_og_image(soup))
    if is_ignored_image(og_image):
        og_image = ""

    image_url = extract_first_product_image(soup) or og_image or extract_image_fallback(soup)

    return {
        "identity": stable_identity(name, url),
        "name": name,
        "web_url": normalize_url(url),
        "hierarchy": hierarchy,
        "image_url": image_url,
    }


def main() -> None:
    sitemap_xml = fetch(SITEMAP_URL)
    urls = parse_sitemap(sitemap_xml)

    cleaned_urls = []
    seen = set()
    for url in urls:
        nurl = normalize_url(url)
        if nurl not in seen and is_candidate_category_url(nurl):
            seen.add(nurl)
            cleaned_urls.append(nurl)

    rows = []
    for url in cleaned_urls:
        try:
            row = scrape_category(url)
            if row:
                rows.append(row)
                print("OK  ", url)
            else:
                print("SKIP", url)
        except Exception as e:
            print("ERR ", url, e)

    unique_rows = []
    seen_urls = set()
    for row in rows:
        if row["web_url"] not in seen_urls:
            seen_urls.add(row["web_url"])
            unique_rows.append(row)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["identity", "name", "web_url", "hierarchy", "image_url"],
        )
        writer.writeheader()
        writer.writerows(unique_rows)

    print(f"Hotovo: {OUTPUT_CSV}, řádků: {len(unique_rows)}")


if __name__ == "__main__":
    main()
