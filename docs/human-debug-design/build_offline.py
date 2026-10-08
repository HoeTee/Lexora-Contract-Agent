"""Bundle generated overview and reference HTML into one offline document.

Render overview.md with the HTML skill first, then run:
    python build_offline.py --overview-html index.html \
        --reference-html reference.html --output index.html
"""

import argparse
import base64
from copy import deepcopy
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree, html


REFERENCE_SCRIPT = r"""
(() => {
  function revealReference(hash) {
    let id;
    try {
      id = decodeURIComponent(hash.slice(1));
    } catch {
      return null;
    }
    const target = document.getElementById(id);
    if (!target || !target.closest('#reference')) return null;
    if (id === 'reference') {
      target.querySelectorAll('details').forEach(section => { section.open = true; });
    }
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    return target;
  }

  document.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const link = event.target.closest('a[href^="#"]');
    if (link) revealReference(link.getAttribute('href'));
  });

  function followHash() {
    const target = revealReference(location.hash);
    if (target) requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
  }
  window.addEventListener('hashchange', followHash);
  followHash();
})();
"""


def load_document(path: Path):
    return html.fromstring(path.read_text(encoding="utf-8"))


def embed_images(document, base_dir: Path):
    for image in document.xpath("//img[@src]"):
        source = image.get("src")
        if source.startswith("data:image/"):
            continue
        parts = urlsplit(source)
        if parts.scheme or parts.netloc:
            raise ValueError(f"Expected a local image: {source}")
        asset = (base_dir / unquote(parts.path)).resolve()
        if not asset.is_relative_to(base_dir.resolve()):
            raise ValueError(f"Image is outside the document directory: {source}")
        mime_type = mimetypes.guess_type(asset.name)[0]
        if not mime_type or not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported image type: {source}")
        encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
        image.set("src", f"data:{mime_type};base64,{encoded}")


def reference_section(reference):
    sections = reference.xpath("//section[starts-with(@id, 'panel-')]")
    if not sections:
        raise ValueError("Reference has no generated sections")

    wrapper = etree.Element("section", {"class": "am-panel", "id": "reference"})
    # Reuse the generator's header and panel styles, including theme switching.
    heading = deepcopy(sections[0][0])
    heading.find("span").text = "详"
    heading.find("h2").text = "完整设计 人体 Debug 互动系统设计文档"
    wrapper.append(heading)
    content = etree.SubElement(wrapper, "div", {"class": "am-panel-body"})
    intro = etree.SubElement(content, "div", {"class": "am-md"})
    etree.SubElement(intro, "p").text = "点击章节标题展开或收起。正文、参数与全部 25 张原型图均已内嵌。"
    backlink = etree.SubElement(etree.SubElement(intro, "p"), "a", href="#panel-A")
    backlink.text = "返回设计总览"
    original_intro = reference.xpath("//header[@class='am-head']/div[contains(@class, 'am-intro')]")
    if original_intro:
        retained_intro = deepcopy(original_intro[0])
        for link in retained_intro.xpath(".//a[@href='index.html']"):
            link.set("href", "#panel-A")
        content.append(retained_intro)

    for section in sections:
        detail = deepcopy(section)
        detail.tag = "details"
        for node in detail.iter():
            if node.get("id"):
                node.set("id", "reference-" + node.get("id"))
        summary = detail.find("header")
        summary.tag = "summary"
        etree.SubElement(summary, "span", {"class": "am-panel-meta"}).text = "展开 / 收起"
        content.append(detail)
    return wrapper


def validate_offline(document):
    ids = [node.get("id") for node in document.xpath("//*[@id]")]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate IDs in bundled HTML")
    for link in document.xpath("//a[@href]"):
        href = link.get("href")
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            continue
        if parts.path or (parts.fragment and unquote(parts.fragment) not in ids):
            raise ValueError(f"Unresolved local link: {href}")
    for resource in document.xpath("//*[@src] | //link[@href]"):
        source = resource.get("src") or resource.get("href")
        if not source.startswith("data:"):
            raise ValueError(f"External resource dependency: {source}")


def bundle(overview_path: Path, reference_path: Path, output_path: Path):
    document = load_document(overview_path)
    reference = load_document(reference_path)
    # Re-running the bundler replaces its own generated additions.
    for node in document.xpath("//*[@id='reference' or @id='offline-reference-script']"):
        node.getparent().remove(node)
    embed_images(document, overview_path.parent)
    embed_images(reference, reference_path.parent)

    for link in document.xpath("//a[@href]"):
        parts = urlsplit(link.get("href"))
        if parts.path == "reference.html" and not parts.scheme and not parts.netloc:
            link.set("href", "#reference-" + parts.fragment if parts.fragment else "#reference")

    body = document.xpath("//div[@class='am-doc-body']")
    if len(body) != 1:
        raise ValueError("Expected one generated document body")
    body[0].append(reference_section(reference))
    navigation = document.xpath("//nav[@class='am-toc']")[0]
    for link in navigation.xpath("a[@href='#reference']"):
        navigation.remove(link)
    etree.SubElement(navigation, "a", href="#reference").text = "完整设计 · 离线参考"
    script = etree.SubElement(document.find("body"), "script", id="offline-reference-script")
    script.text = REFERENCE_SCRIPT
    validate_offline(document)
    output_path.write_text(
        html.tostring(document, encoding="unicode", method="html", doctype="<!DOCTYPE html>"),
        encoding="utf-8",
    )
    print(f"Offline HTML: {output_path.resolve()}")
    print(f"Embedded images: {len(document.xpath('//img'))}")
    print(f"Collapsed reference sections: {len(document.xpath('//details'))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overview-html", required=True, type=Path)
    parser.add_argument("--reference-html", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle(args.overview_html, args.reference_html, args.output)


if __name__ == "__main__":
    main()
