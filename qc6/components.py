"""Which part of a page an issue belongs to: the "component" of Template -> Component -> Issue.

Link and image issues take the area of the page the URL was found in (Breadcrumb, Navigation, Header, Footer, Product
listing...; recorded by the parser), except for URLs whose kind says more (a canonical, an hreflang, a JSON-LD URL).
Page-level issues take the component their rule is about (Title, Canonical, Structured data...).
"""
from __future__ import annotations

TYPE_COMPONENT = {
    "CANONICAL": "Canonical", "HREFLANG": "Hreflang", "JSONLD_URL": "Structured data (schema)",
    "SOCIAL_URL": "Open Graph / social meta", "LINK_META": "Head links (prev / next / alternate)",
    "IFRAME": "Embedded frame", "META_REFRESH": "Meta refresh", "FORM": "Form",
}

# rule code prefix -> component (first match wins; longer, more specific prefixes first)
RULE_PREFIX = [
    ("MISSING_TITLE", "Title"), ("MULTIPLE_TITLE", "Title"), ("DUPLICATE_TITLE", "Title"), ("TITLE_", "Title"),
    ("META_DESCRIPTION", "Meta description"), ("MULTIPLE_META_DESCRIPTION", "Meta description"),
    ("DUPLICATE_META_DESCRIPTION", "Meta description"),
    ("H1_", "Headings"), ("MULTIPLE_H1", "Headings"), ("DUPLICATE_H1", "Headings"), ("HEADING_", "Headings"),
    ("MISSING_CANONICAL", "Canonical"), ("MULTIPLE_CANONICALS", "Canonical"), ("CANONICAL", "Canonical"),
    ("HREFLANG", "Hreflang"), ("OPEN_GRAPH", "Open Graph / social meta"),
    ("STRUCTURED_DATA", "Structured data (schema)"), ("SCHEMA_", "Structured data (schema)"),
    ("PRODUCT_", "Product schema"), ("OFFER_", "Product schema"), ("EMI_", "EMI"),
    ("ENV_URL_", "Links"), ("BROKEN_LINK", "Links"), ("REDIRECTED_LINK", "Links"), ("BROKEN_IMAGE", "Images"),
    ("NOT_IN_SITEMAP", "Sitemap"), ("NOINDEX_IN_SITEMAP", "Sitemap"), ("SITEMAP_", "Sitemap"),
    ("VIEWPORT", "Page markup"), ("CHARSET", "Page markup"), ("HTML_", "Page markup"),
    ("URL_", "URL format"), ("TRAILING_SLASH", "URL format"),
    ("SOURCE_", "Page response"), ("SOFT_404", "Page response"), ("SLOW_RESPONSE", "Page response"),
    ("NON_HTML_SOURCE", "Page response"), ("PAGE_TRUNCATED", "Page response"), ("NO_LINKS_EXTRACTED", "Page response"),
    ("THIN_CONTENT", "Content"), ("DUPLICATE_CONTENT", "Content"),
    ("INDEX_SIGNALS", "Indexing directives"), ("PAGE_NOFOLLOW", "Indexing directives"),
    ("INTERNAL_NOFOLLOW", "Indexing directives"), ("MIXED_CONTENT", "Page resources (mixed content)"),
    ("ORPHAN_PAGE", "Internal linking"), ("JS_RENDERING", "Rendering"), ("CLASSIFICATION", "Page classification"),
]


def component(rule: str, element_type: str = "", location: str = "", subject: str = "") -> str:
    rule = (rule or "").upper()
    if rule.startswith("SCHEMA_") and subject:                       # "Offer.price" -> "Offer schema"
        return f"{subject.split('.', 1)[0]} schema"
    et = (element_type or "").upper()
    if et in TYPE_COMPONENT:
        return TYPE_COMPONENT[et]
    if et and et not in ("SOURCE_PAGE", "PAGE") and location:          # a link / image: where on the page it was
        return location if et not in ("IMAGE",) else f"Images · {location}"
    for prefix, name in RULE_PREFIX:
        if rule.startswith(prefix):
            return name
    if et == "IMAGE" or rule.startswith(("IMAGE", "MISSING_ALT", "LARGE_IMAGE", "EMPTY_IMAGE", "INVALID_IMAGE")):
        return "Images"
    return location or "Page"
