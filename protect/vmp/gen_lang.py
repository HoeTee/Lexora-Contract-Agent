from __future__ import annotations

import argparse
import pathlib


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate VMProtect lang .inc files from en.lng")
    parser.add_argument("--lng", type=pathlib.Path, required=True, help="Path to langs/en.lng")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="Output directory (usually core/)")
    args = parser.parse_args()

    keys: list[tuple[str, str]] = []
    for raw in args.lng.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        keys.append((key.strip(), value.strip().replace('"', '\\"')))

    def_lines = ["{"] + [f'\tdefault_values_[ls{k}] = replace_escape_chars("{v}");' for k, v in keys] + ["};"]
    enum_lines = ["enum LangString {"] + [f"\tls{k}," for k, _ in keys] + ["\tlsCNT };"]
    info_lines = [
        "static const struct {",
        "\tsize_t id;",
        "\tconst char *name;",
        "} key_info[] = {",
    ] + [f'\t{{ls{k}, "{k}"}},' for k, _ in keys] + ["};"]

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "lang_def.inc").write_text("\n".join(def_lines) + "\n", encoding="utf-8")
    (args.out / "lang_enum.inc").write_text("\n".join(enum_lines) + "\n", encoding="utf-8")
    (args.out / "lang_info.inc").write_text("\n".join(info_lines) + "\n", encoding="utf-8")
    print(f"generated {len(keys)} keys")


if __name__ == "__main__":
    main()
