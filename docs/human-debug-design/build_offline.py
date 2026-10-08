"""Embed images in a generated HTML page without changing its visible content.

Render overview.md with the HTML skill first, then run:
    python build_offline.py --input-html index.html --output index.html
"""

import argparse
import base64
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import html


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


def validate_offline(document):
    ids = [node.get("id") for node in document.xpath("//*[@id]")]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate IDs in bundled HTML")
    for link in document.xpath("//a[@href]"):
        parts = urlsplit(link.get("href"))
        if parts.scheme or parts.netloc:
            continue
        if parts.path or (parts.fragment and unquote(parts.fragment) not in ids):
            raise ValueError(f"Unresolved local link: {link.get('href')}")
    for resource in document.xpath("//*[@src] | //link[@href]"):
        source = resource.get("src") or resource.get("href")
        if not source.startswith("data:"):
            raise ValueError(f"External resource dependency: {source}")


def bundle(input_path: Path, output_path: Path):
    document = html.fromstring(input_path.read_text(encoding="utf-8"))
    embed_images(document, input_path.parent)
    validate_offline(document)
    output_path.write_text(
        html.tostring(document, encoding="unicode", method="html", doctype="<!DOCTYPE html>"),
        encoding="utf-8",
    )
    print(f"Offline HTML: {output_path.resolve()}")
    print(f"Embedded images: {len(document.xpath('//img'))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-html", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle(args.input_html, args.output)


if __name__ == "__main__":
    main()
