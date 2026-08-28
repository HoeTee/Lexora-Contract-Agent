from __future__ import annotations

import argparse
import csv
import hashlib
import html
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


DEFAULT_SOURCE = Path(r"C:\Users\lenovo\Downloads\合同样例") / (
    "【已审核】（262号）产品单次销售合同-卡券、实物、定点配送（东福、东乐通用型模版）.docx"
)
SUITE_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SUITE_ROOT / "cases" / "D01.docx"
DEFAULT_EXPECTED_CSV = SUITE_ROOT / "expected_cases_D01.csv"
DEFAULT_CHANGES_CSV = SUITE_ROOT / "xml_changes_D01.csv"
DOCUMENT_XML = "word/document.xml"
TEXT_NODE = re.compile(rb"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.DOTALL)


@dataclass(frozen=True)
class TextReplacement:
    criterion_ids: tuple[int, ...]
    text_node_index: int
    expected_before: str
    after: str
    reason: str


REPLACEMENTS = (
    TextReplacement((4,), 1, "销售合同", "采购销售合同", "使合同名称出现采购二字"),
    TextReplacement((1,), 5, "浙江省杭州市上城区秋涛路660号", "【未填写】", "删除甲方联系地址"),
    TextReplacement((1,), 9, "上海市长宁区武夷路656-658号2幢2层2180室", "【未填写】", "删除乙方联系地址"),
    TextReplacement(
        (16,),
        10,
        "甲方向乙方采购本合同产品向员工发放，经双方友好协商一致，签订本合同并共同信守。",
        "甲方作为采购人向乙方采购本合同产品向员工发放，乙方作为中标单位履行本合同，双方共同信守。",
        "植入采购人和中标单位敏感词",
    ),
    TextReplacement((5, 6), 12, "合同价款及结算方式", "合同金额及支付方式", "使标题出现金额和支付方式"),
    TextReplacement(
        (14,),
        25,
        "88,000.00元",
        "88,000.00元（大写：人民币捌万玖仟元整）",
        "制造数字金额与大写金额不一致",
    ),
    TextReplacement(
        (6,),
        30,
        "合同签订后30\u00a0日内且不晚于2025年09月18日\u00a0，甲方需支付全部合同价款。",
        "合同签订后30日内，甲方一次性支付全部合同价款，具体付款节点另行通知。",
        "制造一次性付款和节点不清问题",
    ),
    TextReplacement(
        (6,),
        42,
        "3.乙方按照甲方指定采购用于兑换的商品开具发票，发票类型为增值税普通发票。",
        "3.乙方在收到全部合同价款后开具发票，发票类型由乙方另行确定。",
        "删除明确发票类型",
    ),
    TextReplacement(
        (15,),
        48,
        "产品兑换范围以官方平台显示为准，本合同采购产品以实际兑换为准，如遇缺货乙方可使用同等价值品类产品替换",
        "产品兑换范围应保持一致一致，如遇缺货，及时使用同等价值产品替换",
        "植入重复词和主语缺失语病",
    ),
    TextReplacement(
        (12,),
        52,
        " 乙方应当于合同生效后3\u00a0个工作日内向甲方发货。",
        " 乙方应当于合同生效后3个工作日内向甲方发贷。",
        "植入发贷错别字",
    ),
    TextReplacement(
        (11, 18),
        67,
        "6.甲方可向乙方定制产品的外观设计，外观设计费用由双方协商确定，计入合同价款。如甲方提供外观设计或部分素材的，需确保其不侵犯其他第三方的合法权益；如乙方因此遭受第三方侵权索赔或者产生对外赔付的，甲方应承担全部责任。外观设计标准由双方协商确定，可作为本合同附件。",
        "正文约定产品兑换有效期截止日为2028年08月31日，具体内容见附件一《产品清单》。",
        "建立正文与附件的期限及名称比对关系",
    ),
    TextReplacement(
        (8,),
        83,
        "1.任何一方不履行合同义务或者履行合同义务不符合约定的，应当继续履行或采取补救措施，给对方造成经济损失的，应承担赔偿责任。",
        "1.任何一方发生违约时，由双方根据实际情况另行协商处理。",
        "删除具体违约责任和兜底责任",
    ),
    TextReplacement((13,), 87, "五、不可抗力", "二、不可抗力", "制造标题编号和顺序混乱"),
    TextReplacement((18,), 104, "七、其他", "附件一《服务清单》", "使实际附件名称与正文引用名称不一致"),
    TextReplacement((11,), 107, "本合同自各方盖章之日起生效，", "附件约定产品兑换有效期截止日为", "将现有文字改为附件期限"),
    TextReplacement((11,), 108, "双方权利义务履行完毕合同自然终止", "2027年08月31日", "使附件期限与正文期限不一致"),
    TextReplacement(
        (10,),
        132,
        "甲方（盖章）：" + "\u3000" * 17 + "             " + "乙方（盖章）：" + "\u3000" * 10,
        "甲方（浙江农商联合银行股份有限公司盖章）：　　　　　　乙方（上海票师付网络科技有限公司盖章）：",
        "使甲方签署名称与合同首部甲方名称不一致",
    ),
)


EXPECTED_CASES = (
    (1, "合同主体基本信息不完整", "合同首部", "地  址: 【未填写】", "xml_text_replace"),
    (2, "缺少合同框架要求的必备标题", "全文标题", "原合同缺少知识产权、验收等必备标题", "existing_issue"),
    (3, "缺少项目负责人及姓名、身份证号、电话号码", "全文", "全文不存在项目负责人信息", "existing_issue"),
    (4, "采购合同应提示采购内容需明确", "合同标题", "采购销售合同", "xml_text_replace"),
    (5, "金额条款需要明确数量、价款、单位、单价、总价和币种", "合同金额及支付方式", "合同金额及支付方式", "xml_text_replace"),
    (6, "支付方式只有一期且付款节点和发票类型不明确", "合同金额及支付方式", "甲方一次性支付全部合同价款", "xml_text_replace"),
    (7, "缺少明确的合同整体履行期限", "全文", "原合同没有明确整体履行起止日期", "existing_issue"),
    (8, "违约责任缺少具体责任和兜底条款", "违约责任", "由双方根据实际情况另行协商处理", "xml_text_replace"),
    (9, "缺少甲方住所地人民法院约定", "争议解决", "乙方所在地人民法院", "existing_issue"),
    (10, "合同首部与签署栏甲方名称不一致", "签署栏", "浙江农商联合银行股份有限公司", "xml_text_replace"),
    (11, "正文与附件对产品兑换有效期约定不一致", "正文及附件", "2028年08月31日 / 2027年08月31日", "xml_text_replace"),
    (12, "存在错别字", "交付约定", "向甲方发贷", "xml_text_replace"),
    (13, "标题编号和顺序混乱", "正文标题", "二、不可抗力", "xml_text_replace"),
    (14, "合同金额大小写不一致", "金额", "88,000.00元 / 人民币捌万玖仟元整", "xml_text_replace"),
    (15, "存在重复词和主语缺失语病", "产品说明", "一致一致", "xml_text_replace"),
    (16, "存在招投标流程敏感词", "合同前言", "采购人、中标单位", "xml_text_replace"),
    (17, "正文结尾缺少以下无正文或以下为合同签署栏", "正文结尾", "全文不存在两种规范结尾", "existing_issue"),
    (18, "正文引用附件名称与实际附件名称不一致", "正文及附件", "产品清单 / 服务清单", "xml_text_replace"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="仅修改 word/document.xml 现有文字节点，生成 D01 测试合同。")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-csv", type=Path, default=DEFAULT_EXPECTED_CSV)
    parser.add_argument("--changes-csv", type=Path, default=DEFAULT_CHANGES_CSV)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_nodes(xml_bytes: bytes) -> list[re.Match[bytes]]:
    return list(TEXT_NODE.finditer(xml_bytes))


def decoded_text(match: re.Match[bytes]) -> str:
    return html.unescape(match.group(2).decode("utf-8"))


def patch_document_xml(xml_bytes: bytes) -> tuple[bytes, list[dict[str, str | int]]]:
    matches = text_nodes(xml_bytes)
    replacements_by_index = {item.text_node_index: item for item in REPLACEMENTS}
    if len(replacements_by_index) != len(REPLACEMENTS):
        raise ValueError("duplicate text_node_index in replacement table")

    chunks: list[bytes] = []
    cursor = 0
    audit_rows: list[dict[str, str | int]] = []
    for index, match in enumerate(matches):
        replacement = replacements_by_index.get(index)
        if replacement is None:
            continue
        before = decoded_text(match)
        if before != replacement.expected_before:
            raise ValueError(
                f"text node T{index} changed in source; expected {replacement.expected_before!r}, got {before!r}"
            )
        escaped_after = escape(replacement.after).encode("utf-8")
        chunks.append(xml_bytes[cursor : match.start(2)])
        chunks.append(escaped_after)
        cursor = match.end(2)
        audit_rows.append(
            {
                "contract_id": "D01",
                "criterion_ids": ",".join(str(value) for value in replacement.criterion_ids),
                "xml_part": DOCUMENT_XML,
                "text_node_index": index,
                "before_text": before,
                "after_text": replacement.after,
                "reason": replacement.reason,
            }
        )
    chunks.append(xml_bytes[cursor:])
    if len(audit_rows) != len(REPLACEMENTS):
        missing = sorted(set(replacements_by_index) - {int(row["text_node_index"]) for row in audit_rows})
        raise ValueError(f"replacement text nodes were not found: {missing}")
    return b"".join(chunks), audit_rows


def structure_mask(xml_bytes: bytes) -> bytes:
    return TEXT_NODE.sub(lambda match: match.group(1) + b"__TEXT__" + match.group(3), xml_bytes)


def clone_with_document_xml(source: Path, target: Path, patched_xml: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"{target.stem}.", suffix=".tmp.docx", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(source, "r") as source_zip, zipfile.ZipFile(temporary, "w") as target_zip:
            target_zip.comment = source_zip.comment
            for info in source_zip.infolist():
                content = source_zip.read(info.filename)
                if info.filename == DOCUMENT_XML:
                    content = patched_xml
                target_zip.writestr(info, content)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def validate_package(source: Path, output: Path, original_xml: bytes, patched_xml: bytes) -> None:
    if structure_mask(original_xml) != structure_mask(patched_xml):
        raise ValueError("document.xml structure changed; only w:t text is allowed to differ")
    original_nodes = text_nodes(original_xml)
    patched_nodes = text_nodes(patched_xml)
    if len(original_nodes) != len(patched_nodes):
        raise ValueError("w:t node count changed")

    allowed = {item.text_node_index for item in REPLACEMENTS}
    actual = {
        index
        for index, (before, after) in enumerate(zip(original_nodes, patched_nodes, strict=True))
        if before.group(2) != after.group(2)
    }
    if actual != allowed:
        raise ValueError(f"unexpected changed text nodes: expected={sorted(allowed)}, actual={sorted(actual)}")

    with zipfile.ZipFile(source, "r") as source_zip, zipfile.ZipFile(output, "r") as output_zip:
        source_names = source_zip.namelist()
        output_names = output_zip.namelist()
        if source_names != output_names:
            raise ValueError("DOCX package part list changed")
        for name in source_names:
            if name == DOCUMENT_XML:
                continue
            source_hash = sha256_bytes(source_zip.read(name))
            output_hash = sha256_bytes(output_zip.read(name))
            if source_hash != output_hash:
                raise ValueError(f"non-document XML part changed: {name}")


def write_changes_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    fieldnames = (
        "contract_id",
        "criterion_ids",
        "xml_part",
        "text_node_index",
        "before_text",
        "after_text",
        "reason",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_expected_csv(path: Path, source: Path, output: Path) -> None:
    source_hash = sha256_bytes(source.read_bytes())
    output_hash = sha256_bytes(output.read_bytes())
    fieldnames = (
        "case_id",
        "contract_id",
        "contract_file",
        "source_file",
        "criterion_id",
        "expected_problem",
        "expected_location",
        "expected_quote",
        "case_origin",
        "source_sha256",
        "modified_sha256",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for criterion_id, problem, location, quote, origin in EXPECTED_CASES:
            writer.writerow(
                {
                    "case_id": f"D01-C{criterion_id:02d}",
                    "contract_id": "D01",
                    "contract_file": output.name,
                    "source_file": source.name,
                    "criterion_id": criterion_id,
                    "expected_problem": problem,
                    "expected_location": location,
                    "expected_quote": quote,
                    "case_origin": origin,
                    "source_sha256": source_hash,
                    "modified_sha256": output_hash,
                }
            )


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    expected_csv = args.expected_csv.resolve()
    changes_csv = args.changes_csv.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source DOCX was not found: {source}")
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"output already exists; use --overwrite: {output}")

    with zipfile.ZipFile(source, "r") as package:
        original_xml = package.read(DOCUMENT_XML)
    patched_xml, audit_rows = patch_document_xml(original_xml)
    clone_with_document_xml(source, output, patched_xml)
    validate_package(source, output, original_xml, patched_xml)
    write_changes_csv(changes_csv, audit_rows)
    write_expected_csv(expected_csv, source, output)

    print(f"Source: {source}")
    print(f"Output: {output}")
    print(f"Changed w:t nodes: {len(audit_rows)}")
    print(f"Expected cases: {len(EXPECTED_CASES)}")
    print("Validation: all non-document.xml parts are byte-identical")
    print("Validation: document.xml structure is unchanged")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
