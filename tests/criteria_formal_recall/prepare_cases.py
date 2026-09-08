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
from xml.etree import ElementTree


SOURCE_ROOT = Path(r"C:\Users\lenovo\Downloads\合同样例")
TEMPLATE_ROOT = Path(r"C:\Users\lenovo\Downloads\模板合同")
SUITE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SUITE_ROOT.parents[1]
DEFAULT_OUTPUT_DIR = SUITE_ROOT / "cases"
DEFAULT_EXPECTED_CSV = SUITE_ROOT / "expected_cases_10.csv"
DEFAULT_CHANGES_CSV = SUITE_ROOT / "xml_changes_10.csv"
FULL_OUTPUT_DIR = SUITE_ROOT / "cases_32"
FULL_EXPECTED_CSV = SUITE_ROOT / "expected_cases_32.csv"
FULL_CHANGES_CSV = SUITE_ROOT / "xml_changes_32.csv"
DOCUMENT_XML = "word/document.xml"
TEXT_NODE = re.compile(rb"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.DOTALL)


CASE_SOURCES = (
    (
        "D01",
        "【已审查】（214号）浙江农商与移动浙江公司集团固话业务协议-修订4.docx",
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
    (3, "项目负责人姓名、身份证号和电话号码未填写", "项目负责人", "项目负责人：姓名【未填写】"),
    (4, "采购合同首部缺少采购内容提示语", "合同首部", "合同名称含采购但没有指定提示语"),
    (5, "金额标题触发后合同首部缺少金额提示语", "合同首部", "标题包含合同金额"),
    (6, "付款计划只有一期且节点、尾款和发票类型不明确", "支付方式", "甲方一次性支付全部合同价款"),
    (7, "合同整体履行期限约定不明确", "合同期限", "具体起止日期由双方另行协商"),
    (8, "违约责任标题触发后合同首部缺少指定提示语", "合同首部", "标题包含违约责任"),
    (9, "争议解决未约定甲方住所地人民法院", "争议解决", "全文不存在甲方住所地人民法院"),
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
    TextReplacement(
        (3,),
        0,
        "                          ",
        "项目负责人：姓名【未填写】，身份证号码【未填写】，电话号码【未填写】",
        "植入信息不完整的项目负责人字段",
    ),
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
        (7,),
        99,
        "（5）甲方有违反本协议任何条款的行为并且在乙方限定的期限内未完成整改或其整改未能达到乙方要求时；",
        "合同履行期限由双方另行协商，起止日期不明确。",
        "制造整体履行期限不明确",
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
    TextReplacement((18,), 133, "时间：", "", "移除附件标题所在段落的前置文字"),
    TextReplacement(
        (11, 18),
        134,
        "　　　　时间：",
        "附件一：服务清单；附件有效期至2027年08月31日。",
        "制造附件名称及正文附件期限不一致",
    ),
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
    parser.add_argument("--case-set", choices=("10", "32"), default="10")
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--template-root", type=Path, default=TEMPLATE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--expected-csv", type=Path, default=DEFAULT_EXPECTED_CSV)
    parser.add_argument("--changes-csv", type=Path, default=DEFAULT_CHANGES_CSV)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def selected_case_sources(args: argparse.Namespace) -> tuple[tuple[str, Path], ...]:
    source_root = args.source_root.resolve()
    selected = [(contract_id, source_root / filename) for contract_id, filename in CASE_SOURCES]
    if args.case_set == "10":
        return tuple(selected)

    template_root = args.template_root.resolve()
    available = sorted(
        (
            path.resolve()
            for root in (source_root, template_root)
            for path in root.glob("*.docx")
            if not path.name.startswith("~$")
        ),
        key=lambda path: (str(path.parent).casefold(), path.name.casefold()),
    )
    if len(available) != 32:
        raise ValueError(
            "32-contract case set requires exactly 32 DOCX files across "
            f"{source_root} and {template_root}; found {len(available)}"
        )

    selected_paths = {path for _, path in selected}
    missing = [path for _, path in selected if path not in available]
    if missing:
        raise FileNotFoundError(f"existing 10-contract source files are missing: {missing}")
    remaining = [path for path in available if path not in selected_paths]
    ordered = [path for _, path in selected] + remaining
    return tuple((f"D{index:02d}", path) for index, path in enumerate(ordered, start=1))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def text_nodes(xml_bytes: bytes) -> list[re.Match[bytes]]:
    return list(TEXT_NODE.finditer(xml_bytes))


def decoded_text_nodes(xml_bytes: bytes) -> list[str]:
    return [html.unescape(match.group(2).decode("utf-8")) for match in text_nodes(xml_bytes)]


def select_contract_title_group(
    values: list[str],
    paragraph_groups: tuple[tuple[int, ...], ...],
    frontmatter_end: int,
) -> tuple[int, ...]:
    """Select the actual cover-title paragraph instead of a later contract clause."""
    upper = min(len(values), max(12, frontmatter_end), 120)
    candidates: list[tuple[int, ...]] = []
    for group in paragraph_groups:
        if not group or group[0] >= upper:
            continue
        value = "".join(values[index] for index in group).strip()
        if not 4 <= len(value) <= 80:
            continue
        if "合同号" in value or value.endswith(("合同：", "协议：")):
            continue
        if "合同" not in value and "协议" not in value:
            continue
        candidates.append(group)
    if not candidates:
        candidates = [
            group
            for group in paragraph_groups
            if group
            and group[0] < upper
            and 4 <= len("".join(values[index] for index in group).strip()) <= 80
            and "合同号" not in "".join(values[index] for index in group)
        ]
    if not candidates:
        raise ValueError("no usable title was found in frontmatter")
    return min(
        candidates,
        key=lambda group: (
            0
            if "".join(values[index] for index in group)
            .strip()
            .endswith(("合同", "协议", "协议书"))
            else 1,
            group[0],
        ),
    )


def generic_replacements(
    contract_id: str,
    values: list[str],
    body_start_text_node: int,
    body_end_text_node: int,
    attachment_start_text_node: int,
    paragraph_groups: tuple[tuple[int, ...], ...],
    structural_heading_groups: set[tuple[int, ...]],
) -> tuple[TextReplacement, ...]:
    used: set[int] = set()
    replacements: dict[int, TextReplacement] = {}
    size = len(values)
    body_fraction = min(0.9, max(0.0, body_start_text_node / size))
    body_end_fraction = min(0.95, max(body_fraction + 0.01, body_end_text_node / size))
    attachment_fraction = min(0.95, max(0.02, attachment_start_text_node / size))
    needs_synthetic_attachment = attachment_start_text_node >= int(size * 0.95)

    def add(index: int, criteria: tuple[int, ...], after: str, reason: str) -> None:
        if after and contract_id in {"D03", "D10"}:
            after = {
                "制造主体基本信息缺失": "地址：【未填写】，联系电话及联系人信息亦未补充。",
                "植入流程敏感词": "采购人甲方向中标单位乙方采购服务，本合同依据招标采购结果订立并由双方共同履行。",
                "制造金额大小写不一致": "合同金额小写：88,000.00元；合同金额大写：人民币捌万玖仟元整。",
                "制造单期付款和节点不明": "甲方一次性支付全部合同价款，付款节点另行通知；不设置其他付款批次，不预留维保尾款，开票和付款时间均由乙方另行确定。",
                "制造整体履行期限不明确": "合同履行期限由双方另行协商，具体起止日期及计算方式均不明确。",
                "制造争议管辖不利于甲方": "本协议争议应先协商；协商不成，任何一方仅可向乙方所在地人民法院起诉，双方排除其他法院管辖。",
                "制造附件名称不一致": "第二十二条 附件" if contract_id == "D03" else after,
                "制造正文附件期限不一致": (
                    "附件一：服务清单；附件一约定同一服务期限截止日为2027年08月31日。"
                    if contract_id == "D03"
                    else after
                ),
            }.get(reason, after)
        if after and needs_synthetic_attachment:
            after = {
                "制造附件名称不一致": "第二十二条 附件",
                "制造正文附件期限不一致": "附件一：服务清单；附件一约定同一服务期限截止日为2027年08月31日。",
            }.get(reason, after)
        if values[index] == after:
            return
        replacements[index] = TextReplacement(criteria, index, values[index], after, reason)

    def choose_paragraph(
        label: str,
        patterns: tuple[str, ...],
        *,
        start: float,
        end: float,
        min_len: int = 1,
        max_len: int,
        prefer_last: bool = False,
        fallback_to_any: bool = False,
    ) -> tuple[int, ...]:
        lower = int(size * start)
        upper = max(lower + 1, int(size * end))
        candidates: list[tuple[int, ...]] = []
        for group in paragraph_groups:
            if not group or group[0] < lower or group[0] >= upper:
                continue
            if any(index in used for index in group):
                continue
            value = "".join(values[index] for index in group).strip()
            if len(value) < min_len or len(value) > max_len:
                continue
            if patterns and not any(re.search(pattern, value) for pattern in patterns):
                continue
            candidates.append(group)
        if not candidates and patterns and fallback_to_any:
            for group in paragraph_groups:
                if not group or group[0] < lower or group[0] >= upper:
                    continue
                if any(index in used for index in group):
                    continue
                value = "".join(values[index] for index in group).strip()
                if min_len <= len(value) <= max_len:
                    candidates.append(group)
        if not candidates:
            raise ValueError(f"{contract_id}: no paragraph available for {label}")
        body_candidates = [
            group
            for group in candidates
            if not (
                group in structural_heading_groups
                and len("".join(values[index] for index in group).strip()) <= 80
            )
        ]
        if body_candidates:
            candidates = body_candidates
        selected = max(candidates, key=lambda group: group[0]) if prefer_last else min(
            candidates,
            key=lambda group: (len("".join(values[index] for index in group).strip()), group[0]),
        )
        used.update(selected)
        return selected

    def add_paragraph(
        group: tuple[int, ...],
        criteria: tuple[int, ...],
        after: str,
        reason: str,
    ) -> None:
        for position, index in enumerate(group):
            add(index, criteria, after if position == 0 else "", reason)

    def choose_heading(
        label: str,
        patterns: tuple[str, ...],
        *,
        after_index: int | None = None,
        reserve_later: int = 0,
        require_after: bool = False,
        prefer_earliest: bool = False,
        prefer_latest: bool = False,
        single_run_only: bool = False,
        top_level_only: bool = False,
        require_following_paragraph: bool = False,
    ) -> tuple[int, ...]:
        candidates: list[tuple[int, ...]] = []
        for group in paragraph_groups:
            if (
                not group
                or not body_start_text_node
                <= group[0]
                < min(body_end_text_node, attachment_start_text_node)
            ):
                continue
            if single_run_only and len(group) != 1:
                continue
            if any(index in used for index in group):
                continue
            value = "".join(values[index] for index in group).strip()
            if not 2 <= len(value) <= 80:
                continue
            if (
                top_level_only
                and group not in structural_heading_groups
                and not re.match(
                    r"^(?:第[一二三四五六七八九十百0-9]+[条章]|[一二三四五六七八九十百]+、)",
                    value,
                )
            ):
                continue
            has_text_numbering = re.match(
                r"^(?:第[一二三四五六七八九十百0-9]+[条章节]|[一二三四五六七八九十百0-9]+[、.．])",
                value,
            )
            if group not in structural_heading_groups and not has_text_numbering:
                continue
            candidates.append(group)
        if require_following_paragraph:
            candidates = [
                group
                for group in candidates
                if any(
                    paragraph[0] > group[-1]
                    and not any(index in used for index in paragraph)
                    and 8 <= len("".join(values[index] for index in paragraph).strip()) <= 300
                    for paragraph in paragraph_groups
                )
            ]
        if after_index is not None:
            later_candidates = [group for group in candidates if group[0] > after_index]
            if later_candidates or require_after:
                candidates = later_candidates
        if reserve_later:
            reservable = [
                group
                for group in candidates
                if sum(other[0] > group[0] for other in candidates) >= reserve_later
            ]
            if reservable:
                candidates = reservable
        preferred = [
            group
            for group in candidates
            if any(
                re.search(pattern, "".join(values[index] for index in group))
                for pattern in patterns
            )
        ]
        available = preferred or candidates
        if not available:
            raise ValueError(
                f"{contract_id}: no structural heading available for {label}; "
                f"after_index={after_index}, body_end={body_end_text_node}, "
                f"attachment_start={attachment_start_text_node}"
            )
        selected = min(
            available,
            key=(
                (lambda group: -group[0])
                if prefer_latest
                else (lambda group: group[0])
                if prefer_earliest
                else lambda group: (
                    len("".join(values[index] for index in group).strip()),
                    group[0],
                )
            ),
        )
        used.update(selected)
        return selected

    title = select_contract_title_group(values, paragraph_groups, body_start_text_node)
    used.update(title)
    address = choose_paragraph(
        "address",
        (r"地址", r"住所", r"甲方", r"乙方"),
        start=0.0,
        end=body_fraction,
        min_len=4,
        max_len=120,
        fallback_to_any=True,
    )
    intro = choose_paragraph(
        "intro",
        (r"甲方.*乙方", r"乙方.*甲方", r"双方"),
        start=0.0,
        end=min(1.0, body_fraction + 0.08),
        min_len=12,
        max_len=500,
        fallback_to_any=True,
    )
    amount_heading = choose_heading(
        "amount heading",
        (r"合同金额", r"合同价款", r"价款", r"支付", r"付款"),
        reserve_later=3 if needs_synthetic_attachment else 0,
        prefer_earliest=needs_synthetic_attachment,
        top_level_only=needs_synthetic_attachment,
    )
    breach_heading = choose_heading(
        "breach heading",
        () if needs_synthetic_attachment else (r"违约责任",),
        after_index=amount_heading[0],
        reserve_later=2 if needs_synthetic_attachment else 0,
        prefer_earliest=needs_synthetic_attachment,
        top_level_only=needs_synthetic_attachment,
    )
    disorder_heading = choose_heading(
        "disordered heading",
        () if needs_synthetic_attachment else (r"不可抗力", r"争议", r"保密", r"知识产权"),
        after_index=breach_heading[0],
        reserve_later=1 if needs_synthetic_attachment else 0,
        prefer_earliest=needs_synthetic_attachment,
        top_level_only=needs_synthetic_attachment,
    )
    synthetic_attachment_heading: tuple[int, ...] | None = None
    if needs_synthetic_attachment:
        try:
            synthetic_attachment_heading = choose_heading(
                "synthetic attachment heading",
                (),
                after_index=disorder_heading[0],
                require_after=True,
                prefer_latest=True,
                single_run_only=True,
                top_level_only=True,
                require_following_paragraph=True,
            )
        except ValueError:
            synthetic_attachment_heading = choose_heading(
                "synthetic attachment heading",
                (),
                after_index=disorder_heading[0],
                require_after=True,
                prefer_latest=True,
                top_level_only=True,
                require_following_paragraph=True,
            )
    amount_zone_start = (
        amount_heading[0] / size
        if needs_synthetic_attachment
        else max(0.0, amount_heading[0] / size - 0.02)
    )
    amount_zone_end = (
        min(body_end_fraction, attachment_fraction, 0.94, amount_heading[0] / size + 0.35)
        if needs_synthetic_attachment
        else min(body_end_fraction, attachment_fraction, amount_heading[0] / size + 0.18)
    )
    amount_value = choose_paragraph(
        "amount value",
        (r"元", r"￥", r"金额", r"价款"),
        start=amount_zone_start,
        end=amount_zone_end,
        min_len=8,
        max_len=300,
        fallback_to_any=True,
    )
    payment = choose_paragraph(
        "payment",
        (
            r"(?:甲方|乙方).*(?:支付|付款)",
            r"(?:支付|付款).*(?:合同价款|款项|费用|金额)",
            r"款项.*支付",
        ),
        start=amount_zone_start,
        end=amount_zone_end,
        min_len=8,
        max_len=400,
        fallback_to_any=True,
    )
    invoice = choose_paragraph(
        "invoice",
        (r"发票",),
        start=amount_zone_start,
        end=min(1.0, amount_zone_end + 0.15),
        min_len=6,
        max_len=400,
        fallback_to_any=True,
    )
    term = choose_paragraph(
        "term",
        (r"合同期限", r"履行期限", r"有效期", r"服务期", r"期限"),
        start=body_fraction,
        end=min(body_end_fraction, 0.8) if needs_synthetic_attachment else body_end_fraction,
        min_len=6,
        max_len=300,
        fallback_to_any=True,
    )
    grammar = choose_paragraph(
        "grammar",
        (),
        start=body_fraction,
        end=body_end_fraction,
        min_len=12,
        max_len=220,
    )
    typo = choose_paragraph(
        "typo",
        (),
        start=body_fraction,
        end=body_end_fraction,
        min_len=12,
        max_len=220,
    )
    body_attachment = choose_paragraph(
        "body attachment",
        (r"附件",),
        start=max(0.05, body_fraction),
        end=(
            min(body_end_fraction, attachment_fraction, 0.8)
            if needs_synthetic_attachment
            else min(body_end_fraction, attachment_fraction)
        ),
        min_len=12,
        max_len=500,
        fallback_to_any=True,
    )
    breach_body = choose_paragraph(
        "breach body",
        (),
        start=breach_heading[0] / size,
        end=min(body_end_fraction, attachment_fraction, breach_heading[0] / size + 0.18),
        min_len=12,
        max_len=400,
    )
    dispute = choose_paragraph(
        "dispute",
        (r"人民法院", r"管辖", r"争议"),
        start=body_fraction,
        end=body_end_fraction,
        min_len=8,
        max_len=400,
        fallback_to_any=True,
    )
    if needs_synthetic_attachment:
        if synthetic_attachment_heading is None:
            raise AssertionError("synthetic attachment heading was not selected")
        try:
            project = choose_paragraph(
                "project owner",
                (),
                start=disorder_heading[0] / size,
                end=synthetic_attachment_heading[0] / size,
                min_len=8,
                max_len=180,
            )
        except ValueError:
            project = choose_paragraph(
                "project owner",
                (),
                start=body_fraction,
                end=synthetic_attachment_heading[0] / size,
                min_len=8,
                max_len=180,
            )
    else:
        try:
            project = choose_paragraph(
                "project owner",
                (),
                start=max(body_fraction, amount_heading[0] / size),
                end=min(body_end_fraction, body_fraction + 0.3),
                min_len=8,
                max_len=180,
            )
        except ValueError:
            project = choose_paragraph(
                "project owner",
                (),
                start=body_fraction,
                end=min(body_end_fraction, body_fraction + 0.3),
                min_len=8,
                max_len=180,
            )
    attachment_name_start = (
        max(
            0.65,
            min(0.90, body_end_fraction - 0.04),
            disorder_heading[0] / size + 0.001,
        )
        if needs_synthetic_attachment
        else attachment_fraction
    )
    attachment_name_end = (
        min(1.0, max(0.97, attachment_name_start + 0.03))
        if needs_synthetic_attachment
        else 1.0
    )
    if needs_synthetic_attachment:
        if synthetic_attachment_heading is None:
            raise AssertionError("synthetic attachment heading was not selected")
        attachment_name = synthetic_attachment_heading
    else:
        attachment_name = choose_paragraph(
            "attachment name",
            (r"附件",),
            start=attachment_name_start,
            end=attachment_name_end,
            min_len=4,
            max_len=180,
            fallback_to_any=True,
        )
    try:
        attachment_date_start = (
            attachment_name[-1] / size
            if needs_synthetic_attachment
            else max(attachment_name_start, attachment_name[0] / size)
        )
        attachment_date = choose_paragraph(
            "attachment date",
            (),
            start=attachment_date_start,
            end=1.0,
            min_len=8,
            max_len=300,
        )
    except ValueError:
        attachment_date = attachment_name
    signature_patterns = (
        r"^甲方[：:].*乙方[：:]",
        r"^甲方\s*[：:].+",
        r"^甲方[：:]$",
        r"甲方.*盖章",
        r"甲方.*签章",
        r"甲方名称.*公章",
        r"甲方.*签字",
    )
    signature_start = max(
        body_fraction,
        body_end_fraction - 0.10,
        (max(body_attachment[-1], dispute[-1], project[-1], grammar[-1], typo[-1]) + 1) / size,
    )
    try:
        signature = choose_paragraph(
            "signature",
            signature_patterns,
            start=signature_start,
            end=body_end_fraction,
            max_len=180,
            prefer_last=True,
            fallback_to_any=True,
        )
    except ValueError:
        signature = choose_paragraph(
            "signature",
            signature_patterns,
            start=0.5,
            end=1.0,
            max_len=180,
            prefer_last=True,
            fallback_to_any=True,
        )
    add_paragraph(title, (4,), "采购合同", "触发采购合同首部提示检查")
    add_paragraph(
        address,
        (1,),
        "".join(values[index] for index in address).strip() + " 地址：【未填写】",
        "制造主体基本信息缺失",
    )
    add_paragraph(intro, (16,), "采购人甲方向中标单位乙方采购服务。", "植入流程敏感词")
    add_paragraph(amount_heading, (5, 6), "一、合同金额及支付方式", "触发金额和支付方式检查")
    add_paragraph(
        amount_value,
        (14,),
        "合同金额小写：88,000.00元；合同金额大写：人民币捌万玖仟元整。",
        "制造金额大小写不一致",
    )
    add_paragraph(payment, (6,), "甲方一次性支付全部合同价款，付款节点另行通知。", "制造单期付款和节点不明")
    add_paragraph(invoice, (6,), "发票类型由乙方另行确定。", "删除明确发票类型")
    add_paragraph(term, (7,), "合同履行期限由双方另行协商，起止日期不明确。", "制造整体履行期限不明确")
    add_paragraph(grammar, (15,), "产品范围保持一致一致。", "植入重复词语病")
    add_paragraph(typo, (12,), "乙方向甲方发贷。", "植入发贷错别字")
    add_paragraph(
        body_attachment,
        (11, 18),
        "正文约定附件一的服务期限截止日为2028年08月31日，附件名称为《产品清单》。",
        "建立正文期限和附件名称",
    )
    add_paragraph(breach_heading, (8,), "四、违约责任", "触发违约责任首部提示检查")
    add_paragraph(breach_body, (8,), "违约责任由双方另行协商。", "弱化违约责任内容")
    add_paragraph(disorder_heading, (13,), "二、不可抗力", "制造标题编号和顺序混乱")
    add_paragraph(dispute, (9,), "协商不成，向乙方所在地人民法院起诉。", "制造争议管辖不利于甲方")
    if attachment_date == attachment_name:
        add_paragraph(
            attachment_name,
            (11, 18),
            "第二十二条 附件一：服务清单；附件一约定同一服务期限截止日为2027年08月31日。",
            "制造附件名称及正文附件期限不一致",
        )
    else:
        add_paragraph(attachment_name, (18,), "附件一：服务清单", "制造附件名称不一致")
        add_paragraph(
            attachment_date,
            (11,),
            "附件一约定同一服务期限截止日为2027年08月31日。",
            "制造正文附件期限不一致",
        )
    add_paragraph(
        signature,
        (10,),
        "甲方：浙江农商联合银行股份有限公司（盖章）　　　　　　乙方：原乙方（盖章）",
        "制造甲方签署名称不一致",
    )
    add_paragraph(
        project,
        (3,),
        "".join(values[index] for index in project)
        + " 项目负责人：姓名【未填写】，身份证号码【未填写】，电话号码【未填写】",
        "植入信息不完整的项目负责人字段",
    )

    return tuple(replacements[index] for index in sorted(replacements))


ABSENCE_REWRITES = (
    ("诚信廉洁合作承诺", "廉洁合作约定", (2,), "移除一个必备合同框架标题"),
    ("甲方住所地人民法院", "乙方所在地人民法院", (9,), "移除甲方住所地人民法院约定"),
    ("以下为合同签署栏", "签字盖章处", (17,), "移除规范正文结束语"),
    ("合同签署栏", "签字盖章处", (17,), "移除易误判的签署栏表述"),
    ("以下无正文", "正文结束", (17,), "移除规范正文结束语"),
)


def enforce_absence_rewrites(
    values: list[str], replacements: tuple[TextReplacement, ...]
) -> tuple[TextReplacement, ...]:
    """Apply whole-contract negative-test invariants after targeted replacements."""
    by_index = {item.text_node_index: item for item in replacements}
    for index, original in enumerate(values):
        existing = by_index.get(index)
        after = existing.after if existing else original
        criteria = set(existing.criterion_ids if existing else ())
        reasons = [existing.reason] if existing else []
        for before_text, after_text, criterion_ids, reason in ABSENCE_REWRITES:
            if before_text not in after:
                continue
            after = after.replace(before_text, after_text)
            criteria.update(criterion_ids)
            reasons.append(reason)
        if after != original:
            by_index[index] = TextReplacement(
                tuple(sorted(criteria)),
                index,
                original,
                after,
                "; ".join(dict.fromkeys(reasons)),
            )
    return tuple(by_index[index] for index in sorted(by_index))


def validate_expected_problems(contract_id: str, patched_xml: bytes) -> None:
    """Fail generation when a declared planted problem is absent from the final XML text."""
    text = "\n".join(decoded_text_nodes(patched_xml))
    required_fragments = (
        "【未填写】",
        "项目负责人：姓名【未填写】",
        "采购",
        "合同金额",
        "一次性支付全部合同价款",
        "违约责任",
        "发贷",
        "二、不可抗力",
        "88,000.00元",
        "人民币捌万玖仟元整",
        "一致一致",
        "采购人",
        "中标单位",
        "2028年08月31日",
        "2027年08月31日",
        "产品清单",
        "服务清单",
    )
    missing = [fragment for fragment in required_fragments if fragment not in text]
    forbidden_fragments = ("甲方住所地人民法院", "以下无正文", "以下为合同签署栏")
    unexpected = [fragment for fragment in forbidden_fragments if fragment in text]
    required_titles = (
        "保密条款",
        "验收",
        "知识产权",
        "甲方义务",
        "乙方义务",
        "诚信廉洁合作承诺",
        "适用法律及合同争议解决",
        "双方约定的其他事项",
    )
    if all(title in text for title in required_titles):
        unexpected.append("合同框架未制造必备标题缺失")
    if missing or unexpected:
        raise ValueError(
            f"{contract_id}: planted-problem validation failed; "
            f"missing={missing}, unexpected={unexpected}"
        )


def region_start_text_node(
    source: Path,
    document_xml: bytes,
    node_prefix: str,
    fallback_fraction: float,
) -> int:
    """Map a DocxIndex region's first body child back to the global w:t index."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from tools.docxindex import build_document_index

    index = build_document_index(
        source,
        llm_expand=False,
        llm_summary=False,
        llm_attachment_hierarchy=False,
    ).to_json_dict()
    starts = [
        int(node["start_index"])
        for node in index.get("nodes") or []
        if str(node.get("node_id") or "").startswith(node_prefix)
        and node.get("start_index") is not None
    ]
    if not starts:
        return int(len(text_nodes(document_xml)) * fallback_fraction)

    first_region_child = min(starts)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ElementTree.fromstring(document_xml)
    body = root.find(f".//{namespace}body")
    if body is None:
        raise ValueError(f"{source.name}: word/document.xml has no w:body")
    text_index = 0
    for body_child_index, child in enumerate(list(body), start=1):
        if body_child_index >= first_region_child:
            return text_index
        text_index += len(child.findall(f".//{namespace}t"))
    return int(len(text_nodes(document_xml)) * fallback_fraction)


def attachment_start_text_node(source: Path, document_xml: bytes) -> int:
    return region_start_text_node(source, document_xml, "attachments/", 0.96)


def body_start_text_node(source: Path, document_xml: bytes) -> int:
    return region_start_text_node(source, document_xml, "body/sec_", 0.2)


def body_end_text_node(source: Path, document_xml: bytes) -> int:
    attachment_start = attachment_start_text_node(source, document_xml)
    return region_start_text_node(
        source,
        document_xml,
        "tail",
        attachment_start / max(1, len(text_nodes(document_xml))),
    )


def paragraph_text_node_indices(document_xml: bytes) -> set[int]:
    """Return global w:t indexes for single-run direct body paragraphs."""
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ElementTree.fromstring(document_xml)
    body = root.find(f".//{namespace}body")
    if body is None:
        return set()
    result: set[int] = set()
    text_index = 0
    for child in list(body):
        count = len(child.findall(f".//{namespace}t"))
        if child.tag == f"{namespace}p" and count == 1:
            result.add(text_index)
        text_index += count
    return result


def paragraph_text_node_groups(document_xml: bytes) -> tuple[tuple[int, ...], ...]:
    """Return global w:t index groups for each direct body paragraph."""
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ElementTree.fromstring(document_xml)
    body = root.find(f".//{namespace}body")
    if body is None:
        return ()
    result: list[tuple[int, ...]] = []
    text_index = 0
    for child in list(body):
        count = len(child.findall(f".//{namespace}t"))
        if child.tag == f"{namespace}p" and count:
            result.append(tuple(range(text_index, text_index + count)))
        text_index += count
    return tuple(result)


def structural_heading_text_node_groups(document_xml: bytes) -> set[tuple[int, ...]]:
    """Return paragraph groups carrying Word numbering or outline-level metadata."""
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ElementTree.fromstring(document_xml)
    body = root.find(f".//{namespace}body")
    if body is None:
        return set()
    result: set[tuple[int, ...]] = set()
    text_index = 0
    for child in list(body):
        count = len(child.findall(f".//{namespace}t"))
        if child.tag == f"{namespace}p" and count:
            properties = child.find(f"{namespace}pPr")
            if properties is not None and (
                properties.find(f"{namespace}numPr") is not None
                or properties.find(f"{namespace}outlineLvl") is not None
            ):
                result.add(tuple(range(text_index, text_index + count)))
        text_index += count
    return result


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


def validate_retrieval_regions(contract_id: str, output: Path) -> None:
    """Verify planted evidence lands in the intended DocxIndex regions and titles."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from tools.docxindex import build_document_index

    index = build_document_index(
        output,
        llm_expand=False,
        llm_summary=False,
        llm_attachment_hierarchy=False,
    ).to_json_dict()
    nodes = index.get("nodes") or []
    anchor_records = list((index.get("anchor_map") or {}).values())

    def texts_in(prefix: str) -> list[str]:
        return [
            str(record.get("raw_text") or record.get("text") or "")
            for record in anchor_records
            if str(record.get("node_id") or "").startswith(prefix)
        ]

    frontmatter_texts = texts_in("frontmatter")
    body_texts = texts_in("body/")
    tail_texts = texts_in("tail")
    all_texts = [str(record.get("raw_text") or record.get("text") or "") for record in anchor_records]
    body_titles = [
        str(node.get("title") or "")
        for node in nodes
        if str(node.get("node_id") or "").startswith("body/")
    ]
    required_region_markers = {
        "frontmatter": (
            (frontmatter_texts, "采购"),
            (frontmatter_texts, "地址：【未填写】"),
            (all_texts, "采购人"),
            (all_texts, "中标单位"),
        ),
        "body": (
            (body_texts, "项目负责人：姓名【未填写】"),
            (body_texts, "一次性支付全部合同价款"),
            (body_texts, "合同履行期限由双方另行协商"),
            (all_texts, "发贷"),
            (all_texts, "一致一致"),
        ),
        "tail": ((tail_texts, "浙江农商联合银行股份有限公司（盖章）"),),
    }
    missing_region_markers = [
        f"{region}:{marker}"
        for region, checks in required_region_markers.items()
        for region_texts, marker in checks
        if not any(marker in value for value in region_texts)
    ]
    missing_titles = [
        marker
        for marker in ("合同金额", "违约责任", "二、不可抗力")
        if not any(marker in title for title in body_titles)
    ]
    if missing_region_markers or missing_titles:
        raise ValueError(
            f"{contract_id}: DocxIndex planted-problem validation failed; "
            f"missing_region_markers={missing_region_markers}, missing_body_titles={missing_titles}"
        )

    markers: dict[str, list[str]] = {
        "正文约定附件一的服务期限截止日为2028年08月31日": [],
        "附件一约定同一服务期限截止日为2027年08月31日": [],
        "服务清单": [],
    }
    for record in anchor_records:
        text = str(record.get("raw_text") or record.get("text") or "")
        node_id = str(record.get("node_id") or "")
        for marker in markers:
            if marker in text:
                markers[marker].append(node_id)
    if not any(
        node_id.startswith("body/")
        for node_id in markers["正文约定附件一的服务期限截止日为2028年08月31日"]
    ):
        raise ValueError(f"{contract_id}:正文对比证据未落入body区域")
    if not any(
        node_id.startswith("attachments/")
        for node_id in markers["附件一约定同一服务期限截止日为2027年08月31日"]
    ):
        raise ValueError(f"{contract_id}:附件期限证据未落入attachments区域")
    if not any(node_id.startswith("attachments/") for node_id in markers["服务清单"]):
        raise ValueError(f"{contract_id}:附件名称证据未落入attachments区域")


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    case_sources = selected_case_sources(args)
    output_dir = (
        FULL_OUTPUT_DIR if args.case_set == "32" and args.output_dir == DEFAULT_OUTPUT_DIR else args.output_dir
    ).resolve()
    expected_csv = (
        FULL_EXPECTED_CSV
        if args.case_set == "32" and args.expected_csv == DEFAULT_EXPECTED_CSV
        else args.expected_csv
    ).resolve()
    changes_csv = (
        FULL_CHANGES_CSV
        if args.case_set == "32" and args.changes_csv == DEFAULT_CHANGES_CSV
        else args.changes_csv
    ).resolve()
    expected_rows: list[dict[str, object]] = []
    change_rows: list[dict[str, object]] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for contract_id, source in case_sources:
        target = output_dir / f"{contract_id}.docx"
        if not source.is_file():
            raise FileNotFoundError(f"source DOCX was not found: {source}")
        if target.exists() and not args.overwrite:
            raise FileExistsError(f"output already exists; use --overwrite: {target}")
        with zipfile.ZipFile(source, "r") as package:
            original_xml = package.read(DOCUMENT_XML)
        values = decoded_text_nodes(original_xml)
        replacements = generic_replacements(
            contract_id,
            values,
            body_start_text_node(source, original_xml),
            body_end_text_node(source, original_xml),
            attachment_start_text_node(source, original_xml),
            paragraph_text_node_groups(original_xml),
            structural_heading_text_node_groups(original_xml),
        )
        replacements = enforce_absence_rewrites(values, replacements)
        patched_xml, local_changes = patch_document_xml(original_xml, replacements)
        validate_expected_problems(contract_id, patched_xml)
        clone_with_document_xml(source, target, patched_xml)
        validate_package(source, target, original_xml, patched_xml, replacements)
        validate_retrieval_regions(contract_id, target)
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
