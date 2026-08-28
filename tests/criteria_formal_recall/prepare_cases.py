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


SOURCE_ROOT = Path(r"C:\Users\lenovo\Downloads\合同样例")
SUITE_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SUITE_ROOT / "cases"
DEFAULT_EXPECTED_CSV = SUITE_ROOT / "expected_cases_10.csv"
DEFAULT_CHANGES_CSV = SUITE_ROOT / "xml_changes_10.csv"
DOCUMENT_XML = "word/document.xml"
TEXT_NODE = re.compile(rb"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.DOTALL)


CASE_SOURCES = (
    (
        "D01",
        "【已审核】（262号）产品单次销售合同-卡券、实物、定点配送（东福、东乐通用型模版）.docx",
    ),
    ("D02", "【已审查】（433号）ZRUBXC软件开发类采购合同-人行支付系统重构项目-20251110.docx"),
    (
        "D03",
        "【已审查】（404号）【ZRUB】北京仁聚汇通信息科技有限责任公司通用类资源池外包服务封闭式框架协议采购协议书-金融科技领域.docx",
    ),
    ("D04", "【已审查】（525号）客服外呼系统专用话务平台采购-HT-ZRUB-2025-12-22-09-E106.docx"),
    ("D05", "【20251224已审查】2025年度算力资源服务租赁项目（算力资源一）采购合同1223.docx"),
    ("D06", "【已审查】（524号）IPv6 上网行为管理设备采购合同.docx"),
    ("D07", "【已审查】（362号）2025年浙江农商联合银行零售金融业务运营服务合同.docx"),
    ("D08", "【已审查】（2026年058号）2026年度GPU算力服务租赁采购合同-20260617_批注版.docx"),
    ("D09", "【已审查】采购合同-财富大厦项目室内空气.docx"),
    ("D10", "【已审查】（466号）百度地图平台（GIS）地图数据采购项目合作协议.docx"),
)


EXPECTED_CASES = (
    (1, "合同主体基本信息不完整", "合同首部", "地址：【未填写】"),
    (2, "缺少合同框架要求的必备标题", "全文标题", "至少一个必备标题不存在"),
    (3, "缺少项目负责人及姓名、身份证号、电话号码", "全文", "全文不存在项目负责人信息"),
    (4, "采购合同首部缺少采购内容提示语", "合同首部", "合同名称含采购但没有指定提示语"),
    (5, "金额标题触发后合同首部缺少金额提示语", "合同首部", "标题包含合同金额"),
    (6, "付款计划只有一期且节点、尾款和发票类型不明确", "支付方式", "甲方一次性支付全部合同价款"),
    (7, "合同整体履行期限约定不明确", "合同期限", "具体起止日期由双方另行协商"),
    (8, "违约责任标题触发后合同首部缺少指定提示语", "合同首部", "标题包含违约责任"),
    (9, "争议解决未约定甲方住所地人民法院", "争议解决", "乙方所在地人民法院"),
    (10, "合同首部与签署栏甲方名称不一致", "签署栏", "浙江农商联合银行股份有限公司"),
    (11, "正文与附件期限约定不一致", "正文及附件", "2028年08月31日 / 2027年08月31日"),
    (12, "存在错别字", "交付约定", "向甲方发贷"),
    (13, "标题编号和顺序混乱", "正文标题", "二、不可抗力"),
    (14, "合同金额大小写不一致", "金额", "88,000.00元 / 人民币捌万玖仟元整"),
    (15, "存在重复词语病", "正文", "一致一致"),
    (16, "存在招投标流程敏感词", "合同前言", "采购人、中标单位"),
    (17, "正文结尾缺少规范结束语", "正文结尾", "不存在以下无正文或以下为合同签署栏"),
    (18, "正文引用附件名称与实际附件名称不一致", "正文及附件", "产品清单 / 服务清单"),
)


@dataclass(frozen=True)
class TextReplacement:
    criterion_ids: tuple[int, ...]
    text_node_index: int
    expected_before: str
    after: str
    reason: str


D01_REPLACEMENTS = (
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
    TextReplacement((5, 6), 12, "合同价款及结算方式", "合同金额及支付方式", "触发金额和支付方式检查"),
    TextReplacement((14,), 25, "88,000.00元", "88,000.00元（大写：人民币捌万玖仟元整）", "制造金额大小写不一致"),
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
        "植入重复词语病",
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
        "弱化违约责任内容",
    ),
    TextReplacement((13,), 87, "五、不可抗力", "二、不可抗力", "制造标题编号和顺序混乱"),
    TextReplacement((18,), 104, "七、其他", "附件一《服务清单》", "制造附件名称不一致"),
    TextReplacement((11,), 107, "本合同自各方盖章之日起生效，", "附件约定产品兑换有效期截止日为", "建立附件期限"),
    TextReplacement((11,), 108, "双方权利义务履行完毕合同自然终止", "2027年08月31日", "制造附件期限不一致"),
    TextReplacement(
        (10,),
        132,
        "甲方（盖章）：" + "\u3000" * 17 + "             " + "乙方（盖章）：" + "\u3000" * 10,
        "甲方（浙江农商联合银行股份有限公司盖章）：　　　　　　乙方（上海票师付网络科技有限公司盖章）：",
        "制造甲方签署名称不一致",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成10份criteria-formal XML-only召回测试合同。")
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--expected-csv", type=Path, default=DEFAULT_EXPECTED_CSV)
    parser.add_argument("--changes-csv", type=Path, default=DEFAULT_CHANGES_CSV)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def text_nodes(xml_bytes: bytes) -> list[re.Match[bytes]]:
    return list(TEXT_NODE.finditer(xml_bytes))


def decoded_text_nodes(xml_bytes: bytes) -> list[str]:
    return [html.unescape(match.group(2).decode("utf-8")) for match in text_nodes(xml_bytes)]


def generic_replacements(contract_id: str, values: list[str]) -> tuple[TextReplacement, ...]:
    used: set[int] = set()
    replacements: dict[int, TextReplacement] = {}
    size = len(values)

    def choose(
        label: str,
        patterns: tuple[str, ...] = (),
        *,
        start: float = 0.0,
        end: float = 1.0,
        min_len: int = 1,
        max_len: int | None = None,
        prefer_short: bool = False,
    ) -> int:
        lower = int(size * start)
        upper = max(lower + 1, int(size * end))
        candidates = []
        for index in range(lower, min(size, upper)):
            if index in used:
                continue
            value = values[index].strip()
            if len(value) < min_len or (max_len is not None and len(value) > max_len):
                continue
            if patterns and not any(re.search(pattern, value) for pattern in patterns):
                continue
            candidates.append(index)
        if not candidates and patterns and (start > 0.0 or end < 1.0):
            return choose(
                label,
                patterns,
                start=0.0,
                end=1.0,
                min_len=min_len,
                max_len=max_len,
                prefer_short=prefer_short,
            )
        if not candidates and patterns:
            return choose(
                label,
                (),
                start=start,
                end=end,
                min_len=min_len,
                max_len=max_len,
                prefer_short=prefer_short,
            )
        if not candidates:
            raise ValueError(f"{contract_id}: no XML text node available for {label}")
        candidates.sort(key=lambda index: (len(values[index]), index) if prefer_short else (-len(values[index]), index))
        selected = candidates[0]
        used.add(selected)
        return selected

    def add(index: int, criteria: tuple[int, ...], after: str, reason: str) -> None:
        if contract_id in {"D03", "D10"}:
            after = {
                "制造主体基本信息缺失": "地址：【未填写】，联系电话及联系人信息亦未补充。",
                "植入流程敏感词": "采购人甲方向中标单位乙方采购服务，本合同依据招标采购结果订立并由双方共同履行。",
                "制造金额大小写不一致": "合同总金额88,000.00元（大写：人民币捌万玖仟元整），双方据此办理结算。",
                "制造单期付款和节点不明": "甲方一次性支付全部合同价款，付款节点另行通知；不设置其他付款批次，不预留维保尾款，开票和付款时间均由乙方另行确定。",
                "制造整体履行期限不明确": "合同履行期限由双方另行协商，具体起止日期及计算方式均不明确。",
                "制造争议管辖不利于甲方": "本协议争议应先协商；协商不成，任何一方仅可向乙方所在地人民法院起诉，双方排除其他法院管辖。",
            }.get(reason, after)
        if values[index] == after:
            return
        replacements[index] = TextReplacement(criteria, index, values[index], after, reason)

    title = choose("title", (r"合同", r"协议"), end=0.15, max_len=80, prefer_short=True)
    address = choose("address", (r"地址", r"住所"), end=0.25, min_len=8, max_len=120, prefer_short=True)
    intro = choose(
        "intro", (r"甲方.*乙方", r"乙方.*甲方", r"双方"), end=0.3, min_len=20, prefer_short=True
    )
    amount_heading = choose(
        "amount heading",
        (r"合同金额", r"合同价款", r"价款", r"支付", r"付款"),
        end=0.75,
        min_len=11,
        max_len=80,
        prefer_short=True,
    )
    amount_zone_start = max(0.0, amount_heading / size - 0.02)
    amount_zone_end = min(1.0, amount_heading / size + 0.18)
    amount_value = choose(
        "amount value",
        (),
        start=amount_zone_start,
        end=amount_zone_end,
        min_len=29,
        max_len=100,
        prefer_short=True,
    )
    payment = choose(
        "payment",
        (r"支付", r"付款"),
        start=amount_zone_start,
        end=amount_zone_end,
        min_len=23,
        prefer_short=True,
    )
    invoice = choose(
        "invoice", (r"发票",), start=amount_zone_start, end=amount_zone_end, min_len=12, prefer_short=True
    )
    term = choose(
        "term",
        (r"合同期限", r"履行期限", r"有效期", r"服务期", r"期限"),
        start=0.05,
        end=0.9,
        min_len=23,
        prefer_short=True,
    )
    grammar = choose("grammar", (), start=0.15, end=0.65, min_len=11, prefer_short=True)
    typo = choose("typo", (), start=0.15, end=0.7, min_len=8, prefer_short=True)
    body_attachment = choose(
        "body attachment", (r"附件",), start=0.05, end=0.8, min_len=32, prefer_short=True
    )
    breach_heading = choose("breach heading", (r"违约责任",), start=0.2, end=0.9, max_len=80, prefer_short=True)
    breach_body = choose(
        "breach body",
        (),
        start=breach_heading / size,
        end=min(1.0, breach_heading / size + 0.18),
        min_len=12,
        prefer_short=True,
    )
    disorder_heading = choose(
        "disordered heading", (r"不可抗力", r"争议", r"保密", r"知识产权"), start=0.25, end=0.95, max_len=100, prefer_short=True
    )
    dispute = choose(
        "dispute", (r"人民法院", r"管辖", r"争议"), start=0.35, end=0.98, min_len=19, prefer_short=True
    )
    attachment_name = choose(
        "attachment name", (r"附件",), start=0.45, end=1.0, min_len=9, max_len=160, prefer_short=True
    )
    attachment_date = choose(
        "attachment date", (), start=max(0.5, attachment_name / size), end=1.0, min_len=20, prefer_short=True
    )
    signature = choose(
        "signature",
        (r"^甲方[：:]$", r"甲方.*盖章", r"甲方.*签章", r"甲方名称.*公章", r"甲方.*签字"),
        start=0.55,
        end=1.0,
        max_len=180,
        prefer_short=True,
    )

    add(title, (4,), "采购合同", "触发采购合同首部提示检查")
    add(address, (1,), "地址：【未填写】", "制造主体基本信息缺失")
    add(intro, (16,), "采购人甲方向中标单位乙方采购服务。", "植入流程敏感词")
    add(amount_heading, (5, 6), "一、合同金额及支付方式", "触发金额和支付方式检查")
    add(amount_value, (14,), "88,000.00元（大写：人民币捌万玖仟元整）", "制造金额大小写不一致")
    add(payment, (6,), "甲方一次性支付全部合同价款，付款节点另行通知。", "制造单期付款和节点不明")
    add(invoice, (6,), "发票类型由乙方另行确定。", "删除明确发票类型")
    add(term, (7,), "合同履行期限由双方另行协商，起止日期不明确。", "制造整体履行期限不明确")
    add(grammar, (15,), "产品范围保持一致一致。", "植入重复词语病")
    add(typo, (12,), "乙方向甲方发贷。", "植入发贷错别字")
    add(body_attachment, (11, 18), "正文有效期至2028年08月31日，见附件一《产品清单》。", "建立正文期限和附件名称")
    add(breach_heading, (8,), "四、违约责任", "触发违约责任首部提示检查")
    add(breach_body, (8,), "违约责任由双方另行协商。", "弱化违约责任内容")
    add(disorder_heading, (13,), "二、不可抗力", "制造标题编号和顺序混乱")
    add(dispute, (9,), "协商不成，向乙方所在地人民法院起诉。", "制造争议管辖不利于甲方")
    add(attachment_name, (18,), "附件一《服务清单》", "制造附件名称不一致")
    add(attachment_date, (11,), "附件有效期至2027年08月31日。", "制造正文附件期限不一致")
    add(signature, (10,), "甲方（浙农商盖章）：", "制造甲方签署名称不一致")

    forbidden_rewrites = (
        ("项目负责人", "项目联系人", (3,), "移除项目负责人字段"),
        ("以下无正文", "正文结束", (17,), "移除规范正文结束语"),
        ("以下为合同签署栏", "合同签署栏", (17,), "移除规范正文结束语"),
    )
    for index, value in enumerate(values):
        if index in replacements:
            continue
        after = value
        criteria: set[int] = set()
        reasons = []
        for before_text, after_text, criterion_ids, reason in forbidden_rewrites:
            if before_text in after:
                after = after.replace(before_text, after_text)
                criteria.update(criterion_ids)
                reasons.append(reason)
        if after != value:
            replacements[index] = TextReplacement(
                tuple(sorted(criteria)), index, value, after, "; ".join(sorted(set(reasons)))
            )

    return tuple(replacements[index] for index in sorted(replacements))


def patch_document_xml(
    xml_bytes: bytes, replacements: tuple[TextReplacement, ...]
) -> tuple[bytes, list[dict[str, str | int]]]:
    replacements_by_index = {item.text_node_index: item for item in replacements}
    chunks: list[bytes] = []
    cursor = 0
    audit_rows: list[dict[str, str | int]] = []
    for index, match in enumerate(text_nodes(xml_bytes)):
        chunks.append(xml_bytes[cursor : match.start()])
        replacement = replacements_by_index.get(index)
        if replacement is None:
            chunks.append(match.group(0))
        else:
            before = html.unescape(match.group(2).decode("utf-8"))
            if before != replacement.expected_before:
                raise ValueError(
                    f"text node {index} mismatch: expected={replacement.expected_before!r}, actual={before!r}"
                )
            replacement_text = escape(replacement.after, {'"': "&quot;"}).encode("utf-8")
            chunks.extend((match.group(1), replacement_text, match.group(3)))
            audit_rows.append(
                {
                    "criterion_ids": ";".join(str(value) for value in replacement.criterion_ids),
                    "xml_part": DOCUMENT_XML,
                    "text_node_index": index,
                    "before_text": before,
                    "after_text": replacement.after,
                    "reason": replacement.reason,
                }
            )
        cursor = match.end()
    chunks.append(xml_bytes[cursor:])
    if len(audit_rows) != len(replacements):
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


def validate_package(
    source: Path,
    output: Path,
    original_xml: bytes,
    patched_xml: bytes,
    replacements: tuple[TextReplacement, ...],
) -> None:
    if structure_mask(original_xml) != structure_mask(patched_xml):
        raise ValueError("document.xml structure changed; only w:t text is allowed to differ")
    original_nodes = text_nodes(original_xml)
    patched_nodes = text_nodes(patched_xml)
    if len(original_nodes) != len(patched_nodes):
        raise ValueError("w:t node count changed")
    allowed = {item.text_node_index for item in replacements}
    actual = {
        index
        for index, (before, after) in enumerate(zip(original_nodes, patched_nodes, strict=True))
        if before.group(2) != after.group(2)
    }
    if actual != allowed:
        raise ValueError(f"unexpected changed text nodes: expected={sorted(allowed)}, actual={sorted(actual)}")
    with zipfile.ZipFile(source, "r") as source_zip, zipfile.ZipFile(output, "r") as output_zip:
        if source_zip.namelist() != output_zip.namelist():
            raise ValueError("DOCX package part list changed")
        for name in source_zip.namelist():
            if name == DOCUMENT_XML:
                continue
            if sha256_bytes(source_zip.read(name)) != sha256_bytes(output_zip.read(name)):
                raise ValueError(f"non-document XML part changed: {name}")


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    source_root = args.source_root.resolve()
    output_dir = args.output_dir.resolve()
    expected_csv = args.expected_csv.resolve()
    changes_csv = args.changes_csv.resolve()
    expected_rows: list[dict[str, object]] = []
    change_rows: list[dict[str, object]] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for contract_id, filename in CASE_SOURCES:
        source = source_root / filename
        target = output_dir / f"{contract_id}.docx"
        if not source.is_file():
            raise FileNotFoundError(f"source DOCX was not found: {source}")
        if target.exists() and not args.overwrite:
            raise FileExistsError(f"output already exists; use --overwrite: {target}")
        with zipfile.ZipFile(source, "r") as package:
            original_xml = package.read(DOCUMENT_XML)
        values = decoded_text_nodes(original_xml)
        replacements = D01_REPLACEMENTS if contract_id == "D01" else generic_replacements(contract_id, values)
        patched_xml, local_changes = patch_document_xml(original_xml, replacements)
        clone_with_document_xml(source, target, patched_xml)
        validate_package(source, target, original_xml, patched_xml, replacements)
        source_hash = sha256_bytes(source.read_bytes())
        output_hash = sha256_bytes(target.read_bytes())
        for row in local_changes:
            change_rows.append({"contract_id": contract_id, **row})
        for criterion_id, problem, location, quote in EXPECTED_CASES:
            expected_rows.append(
                {
                    "case_id": f"{contract_id}-C{criterion_id:02d}",
                    "contract_id": contract_id,
                    "contract_file": target.name,
                    "source_file": source.name,
                    "criterion_id": criterion_id,
                    "expected_problem": problem,
                    "expected_location": location,
                    "expected_quote": quote,
                    "source_sha256": source_hash,
                    "modified_sha256": output_hash,
                }
            )
        print(f"{contract_id}: nodes={len(values)}, changed={len(local_changes)}, source={source.name}")

    write_csv(
        expected_csv,
        (
            "case_id",
            "contract_id",
            "contract_file",
            "source_file",
            "criterion_id",
            "expected_problem",
            "expected_location",
            "expected_quote",
            "source_sha256",
            "modified_sha256",
        ),
        expected_rows,
    )
    write_csv(
        changes_csv,
        (
            "contract_id",
            "criterion_ids",
            "xml_part",
            "text_node_index",
            "before_text",
            "after_text",
            "reason",
        ),
        change_rows,
    )
    print(f"Expected cases: {len(expected_rows)}")
    print(f"Changed text nodes: {len(change_rows)}")
    print("Validation: only existing word/document.xml w:t text changed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
