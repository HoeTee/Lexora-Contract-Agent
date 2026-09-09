from __future__ import annotations

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}
W_NS = NS["w"]


def w_tag(local_name: str) -> str:
    return f"{{{W_NS}}}{local_name}"


def attr_value(element: etree._Element | None, name: str = "val") -> str | None:
    if element is None:
        return None
    return element.get(w_tag(name))


def xpath_one(element: etree._Element, query: str) -> etree._Element | None:
    result = element.xpath(query, namespaces=NS)
    return result[0] if result else None


def text_of(element: etree._Element) -> str:
    """Return text visible in the current Word revision view."""
    parts: list[str] = []
    for text_element in element.xpath(".//w:t", namespaces=NS):
        if has_ancestor_within(text_element, element, {"del", "moveFrom"}):
            continue
        parts.append(text_element.text or "")
    return "".join(parts).strip()


def has_ancestor_within(
    element: etree._Element,
    stop: etree._Element,
    local_names: set[str],
) -> bool:
    """Return whether an ancestor before ``stop`` has one of the local names."""
    parent = element.getparent()
    while parent is not None and parent is not stop:
        if etree.QName(parent).localname in local_names:
            return True
        parent = parent.getparent()
    return False


def xml_path(element: etree._Element) -> str:
    tree = element.getroottree()
    return tree.getpath(element)
