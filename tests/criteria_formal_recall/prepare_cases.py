from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pythoncom
import win32com.client
from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.table import Table
from docx.text.paragraph import Paragraph


DEFAULT_SOURCE_DIR = Path(r"C:\Users\lenovo\Downloads\合同样例")
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_CSV_PATH = Path(__file__).resolve().parent / "expected_cases.csv"
EXPECTED_CRITERIA = tuple(range(1, 19))
DOCX_FORMAT = 16


@dataclass(frozen=True)
class ContractSource:
    contract_id: str
    filename: str
    category: str


SOURCES = (
    ContractSource(
        "D01",
        "【已审核】（262号）产品单次销售合同-卡券、实物、定点配送（东福、东乐通用型模版）.docx",
        "产品销售",
    ),
    ContractSource(
        "D02",
        "【已审查】浙江农村商业联合银行股份有限公司与中国移动通信集团浙江有限公司战略合作协议.docx",
        "战略合作",
    ),
    ContractSource(
        "D03",
        "【已审查】浙江省企业工资支付监管平台银行合作框架协议2025.docx",
        "平台合作",
    ),
    ContractSource(
        "D04",
        "【已审查】（458号）2025年智能服务深度运营提升培训合作协议.docx",
        "培训服务",
    ),
    ContractSource(
        "D05",
        "【已审查】（433号）ZRUBXC软件开发类采购合同-人行支付系统重构项目-20251110.docx",
        "软件开发采购",
    ),
    ContractSource(
        "D06",
        "【已审查】（312号）关于联合开展普惠金融服务共同富裕课题研究及推广宣传活动服务采购合同（ZRUB-2025-08-28-19-E001）.docx",
        "研究服务采购",
    ),
    ContractSource(
        "D07",
        "【已审查】(464号）浙江省体育局战略合作协议1124.docx",
        "政务合作",
    ),
    ContractSource(
        "D08",
        "【已审查】（524号）IPv6 上网行为管理设备采购合同.docx",
        "设备采购",
    ),
    ContractSource(
        "D09",
        "【已审查】（528号）2026年至2028年贵宾医疗服务合作协议-邵逸夫医院.docx",
        "医疗服务",
    ),
    ContractSource(
        "D10",
        "【已审查】（404号）【ZRUB】北京仁聚汇通信息科技有限责任公司通用类资源池外包服务封闭式框架协议采购协议书-金融科技领域.docx",
        "外包服务采购",
    ),
)


CRITERION_ROWS = (
    (1, "合同主体基本信息不完整", "合同基本信息", "甲乙双方联系地址、联系电话、传真、法定代表人和联系人均留空"),
    (2, "缺少合同框架要求的必备标题", "目录", "目录故意缺少诚信廉洁合作承诺和双方约定的其他事项"),
    (3, "项目负责人信息不完整", "项目负责人", "设置项目负责人栏但姓名、身份证号码和电话号码留空"),
    (4, "采购合同应提示采购内容需明确", "合同标题", "在合同标题中保留采购二字，并使采购内容缺少具体规格信息"),
    (5, "金额条款缺少数量、单价、币种或计量单位", "金额", "目录设置金额标题，金额条款只给出互相矛盾的总价"),
    (6, "支付方式只有一期且付款安排不完整", "支付方式", "设置一次性付款，缺少明确节点、质保尾款和发票类型，并使比例与金额加总不一致"),
    (7, "合同履行期限表述模糊", "合同期限", "期限写为项目完成后适时结束，未给出起止日期或自然日、工作日定义"),
    (8, "违约责任缺少具体责任和兜底条款", "违约责任", "目录保留违约责任标题，正文仅写双方协商处理"),
    (9, "缺少甲方住所地人民法院约定", "争议解决", "争议条款仅约定向乙方所在地有管辖权的法院起诉"),
    (10, "合同主体名称和签署主体名称不一致", "签署栏", "首部甲方名称与签署栏甲方名称不一致"),
    (11, "正文与附件约定不一致", "正文及附件", "正文免费维保期为12个月，附件写为18个月"),
    (12, "存在错别字", "服务要求", "植入履型、验収两个明确错别字"),
    (13, "合同标题顺序杂乱", "目录", "将附件和争议解决放在主要义务、验收等标题之前"),
    (14, "合同金额大小写不一致", "金额", "数字金额与人民币大写金额故意设置为不同数值"),
    (15, "存在重复词和主语缺失等语病", "服务要求", "植入一致一致和缺少主语的句子"),
    (16, "存在招投标流程敏感词", "服务要求", "正文植入招标单位、中标单位、投标人等敏感词之一"),
    (17, "正文结尾缺少以下无正文或以下为合同签署栏", "正文结尾", "删除全文中的两种规范结尾表述"),
    (18, "正文所述附件与实际附件名称不一致", "正文及附件", "正文引用服务方案，实际附件标题为技术清单"),
)


SENSITIVE_TERMS = (
    "招标单位",
    "中标单位",
    "投标单位",
    "谈判单位",
    "谈判响应单位",
    "采购人",
    "投标人",
    "入围单位",
    "征集单位",
    "中标单位",
)

PARTY_B_NAMES = (
    "杭州甲辰数字科技有限公司",
    "浙江乙木信息服务有限公司",
    "宁波丙火企业管理有限公司",
    "温州丁元技术服务有限公司",
    "绍兴戊土软件有限公司",
    "嘉兴己方咨询有限公司",
    "湖州庚金文化服务有限公司",
    "金华辛禾设备有限公司",
    "衢州壬水医疗服务有限公司",
    "台州癸云外包服务有限公司",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="制作 criteria-formal 的10份人工召回测试合同。")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_with_word(source: Path, target: Path) -> None:
    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    document = None
    try:
        document = word.Documents.Open(
            str(source.resolve()),
            ReadOnly=True,
            AddToRecentFiles=False,
            ConfirmConversions=False,
        )
        document.TrackRevisions = False
        if document.Revisions.Count:
            document.Revisions.AcceptAll()
        while document.Comments.Count:
            document.Comments(1).Delete()
        document.SaveAs2(str(target.resolve()), FileFormat=DOCX_FORMAT, AddToRecentFiles=False)
    finally:
        if document is not None:
            document.Close(False)
        word.Quit()
        pythoncom.CoUninitialize()


def iter_table_paragraphs(table: Table) -> Iterable[Paragraph]:
    seen_cells: set[int] = set()
    for row in table.rows:
        for cell in row.cells:
            cell_id = id(cell._tc)
            if cell_id in seen_cells:
                continue
            seen_cells.add(cell_id)
            yield from cell.paragraphs
            for nested in cell.tables:
                yield from iter_table_paragraphs(nested)


def iter_all_paragraphs(document: DocumentObject) -> Iterable[Paragraph]:
    yield from document.paragraphs
    for table in document.tables:
        yield from iter_table_paragraphs(table)
    for section in document.sections:
        for container in (section.header, section.footer):
            yield from container.paragraphs
            for table in container.tables:
                yield from iter_table_paragraphs(table)


def replace_forbidden_phrases(document: DocumentObject) -> None:
    replacements = {
        "甲方住所地人民法院": "乙方所在地有管辖权的人民法院",
        "以下为合同签署栏": "",
        "以下无正文": "",
        "诚信廉洁合作承诺": "合作说明",
        "双方约定的其他事项": "补充说明",
    }
    for paragraph in iter_all_paragraphs(document):
        original = paragraph.text
        updated = original
        for before, after in replacements.items():
            updated = updated.replace(before, after)
        if updated != original:
            paragraph.text = updated


def redact_existing_contact_information(document: DocumentObject) -> None:
    labels = ("联系地址", "地址", "地  址", "联系电话", "电话", "传真", "法定代表人", "联系人")
    for paragraph in iter_all_paragraphs(document):
        text = paragraph.text.strip()
        if not text or "项目负责人" in text:
            continue
        if any(label in text for label in labels):
            paragraph.text = "主体联系信息：联系地址：____    联系电话：____    传真：____    法定代表人：____    联系人：____"


def prepend_paragraph(document: DocumentObject, text: str, *, bold: bool = False, center: bool = False) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run(text)
    run.bold = bold
    if center:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    first = document._body._element[0]
    first.addprevious(paragraph._p)


def append_seeded_content(document: DocumentObject, index: int, contract_id: str) -> dict[int, str]:
    party_b = PARTY_B_NAMES[index]
    sensitive_term = SENSITIVE_TERMS[index]
    numeric_amount = 1_000_000 + index * 100_000
    uppercase_amount = "人民币玖拾万元整" if index % 2 == 0 else "人民币壹佰伍拾万元整"
    percent = 85 + index
    body_attachment_name = f"附件一《{contract_id}服务方案》"
    actual_attachment_name = f"附件一《{contract_id}技术清单》"

    front_lines = [
        (f"{contract_id}信息系统综合服务采购合同", True, True),
        ("合同基本信息", True, False),
        ("甲方：浙江农村商业联合银行股份有限公司", False, False),
        (f"乙方：{party_b}", False, False),
        ("甲方联系地址：____    联系电话：____    传真：____    法定代表人：____    联系人：____", False, False),
        ("乙方联系地址：____    联系电话：____    传真：____    法定代表人：____    联系人：____", False, False),
        ("目录", True, True),
        ("一、附件", False, False),
        ("二、适用法律及合同争议解决", False, False),
        ("三、甲方义务", False, False),
        ("四、保密条款", False, False),
        ("五、支付方式", False, False),
        ("六、金额", False, False),
        ("七、验收", False, False),
        ("八、乙方违约责任", False, False),
        ("九、知识产权", False, False),
        ("十、甲方违约责任", False, False),
        ("十一、不可抗力", False, False),
        ("十二、乙方义务", False, False),
        ("十三、合同的生效及合同份数", False, False),
        ("十四、合同的补充、变更与解除", False, False),
    ]
    for text, bold, center in reversed(front_lines):
        prepend_paragraph(document, text, bold=bold, center=center)

    body_lines = [
        ("项目负责人", True),
        ("项目负责人姓名：____    身份证号码：____    电话号码：____", False),
        ("采购内容", True),
        ("乙方提供相关产品及服务，具体品种、规格、型号、等级和数量由双方后续另行商定。", False),
        ("金额", True),
        (f"本合同含税总价为{numeric_amount}元，大写金额为{uppercase_amount}，具体数量、单价、币种及计量单位另行确定。", False),
        ("支付方式", True),
        (f"甲方在收到乙方付款申请后一次性支付全部价款；付款计划按{percent}%执行，另支付300000元，具体付款时间另行协商。", False),
        ("合同期限", True),
        ("本合同自项目启动之日起生效，至项目完成后适时结束，相关期限按日计算。", False),
        ("违约责任", True),
        ("任何一方发生违约时，由双方另行协商处理。", False),
        ("适用法律及合同争议解决", True),
        ("协商不成的，任一方可向乙方所在地有管辖权的人民法院提起诉讼。", False),
        ("服务要求", True),
        (f"乙方应按约履型服务并完成验収，相关标准应保持一致一致。", False),
        (f"收到通知后及时完成整改，并由{sensitive_term}确认最终结果。", False),
        ("正文约定免费维保期为验收合格之日起12个月。", False),
        (f"本合同正文所称附件为{body_attachment_name}，附件构成本合同不可分割的组成部分。", False),
        ("合同签署", True),
        ("甲方：浙江农商联合银行股份有限公司", False),
        (f"乙方：{party_b}项目部", False),
        ("附件", True),
        (actual_attachment_name, True),
        ("本附件约定免费维保期为验收合格之日起18个月。", False),
    ]
    for text, bold in body_lines:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(text)
        run.bold = bold

    return {
        1: "甲方联系地址：____",
        2: "目录",
        3: "项目负责人姓名：____",
        4: f"{contract_id}信息系统综合服务采购合同",
        5: f"本合同含税总价为{numeric_amount}元",
        6: "一次性支付全部价款",
        7: "至项目完成后适时结束",
        8: "由双方另行协商处理",
        9: "乙方所在地有管辖权的人民法院",
        10: "甲方：浙江农商联合银行股份有限公司",
        11: "免费维保期为验收合格之日起12个月",
        12: "履型服务并完成验収",
        13: "一、附件",
        14: f"大写金额为{uppercase_amount}",
        15: "一致一致",
        16: sensitive_term,
        17: "合同签署",
        18: body_attachment_name,
    }


def mutate_document(normalized: Path, output: Path, index: int, contract_id: str) -> dict[int, str]:
    document = Document(normalized)
    replace_forbidden_phrases(document)
    redact_existing_contact_information(document)
    quotes = append_seeded_content(document, index, contract_id)
    document.save(output)
    return quotes


def validate_docx(path: Path, expected_quotes: dict[int, str]) -> None:
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        document_xml = package.read("word/document.xml").decode("utf-8")
    if "word/comments.xml" in names:
        raise ValueError(f"existing comments were not removed: {path}")
    if re.search(r"<w:(?:ins|del)(?:\s|>)", document_xml):
        raise ValueError(f"tracked revisions were not accepted: {path}")

    reopened = Document(path)
    text = "\n".join(paragraph.text for paragraph in iter_all_paragraphs(reopened))
    for criterion_id, quote in expected_quotes.items():
        if quote not in text:
            raise ValueError(f"criterion {criterion_id} evidence is missing from {path.name}: {quote}")
    for forbidden in ("甲方住所地人民法院", "以下为合同签署栏", "以下无正文", "诚信廉洁合作承诺", "双方约定的其他事项"):
        if forbidden in text:
            raise ValueError(f"forbidden compliant text remains in {path.name}: {forbidden}")


def validate_with_word(path: Path) -> None:
    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    document = None
    try:
        document = word.Documents.Open(str(path.resolve()), ReadOnly=True, AddToRecentFiles=False)
        if document.Comments.Count:
            raise ValueError(f"Word still sees comments in {path.name}")
        if document.Revisions.Count:
            raise ValueError(f"Word still sees revisions in {path.name}")
    finally:
        if document is not None:
            document.Close(False)
        word.Quit()
        pythoncom.CoUninitialize()


def prepare_one(source_dir: Path, output_dir: Path, item: ContractSource, index: int, overwrite: bool) -> tuple[Path, str, str, dict[int, str]]:
    source = source_dir / item.filename
    if not source.is_file():
        raise FileNotFoundError(f"source contract was not found: {source}")
    output = output_dir / f"{item.contract_id}.docx"
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists; use --overwrite: {output}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"criteria_recall_{item.contract_id}_") as temp_dir:
        normalized = Path(temp_dir) / f"{item.contract_id}.normalized.docx"
        staged = Path(temp_dir) / f"{item.contract_id}.mutated.docx"
        normalize_with_word(source, normalized)
        quotes = mutate_document(normalized, staged, index, item.contract_id)
        validate_docx(staged, quotes)
        validate_with_word(staged)
        shutil.copy2(staged, output)
    return output, sha256(source), sha256(output), quotes


def write_expected_csv(path: Path, prepared: list[tuple[ContractSource, Path, str, str, dict[int, str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "case_id",
                "contract_id",
                "contract_file",
                "source_file",
                "category",
                "criterion_id",
                "expected_problem",
                "expected_location",
                "expected_quote",
                "modification",
                "source_sha256",
                "modified_sha256",
            ),
        )
        writer.writeheader()
        for item, output, source_hash, output_hash, quotes in prepared:
            for criterion_id, problem, location, modification in CRITERION_ROWS:
                writer.writerow(
                    {
                        "case_id": f"{item.contract_id}-C{criterion_id:02d}",
                        "contract_id": item.contract_id,
                        "contract_file": output.name,
                        "source_file": item.filename,
                        "category": item.category,
                        "criterion_id": criterion_id,
                        "expected_problem": problem,
                        "expected_location": location,
                        "expected_quote": quotes[criterion_id],
                        "modification": modification,
                        "source_sha256": source_hash,
                        "modified_sha256": output_hash,
                    }
                )


def main() -> int:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    output_dir = args.output_dir.resolve()
    csv_path = args.csv.resolve()
    prepared = []
    for index, item in enumerate(SOURCES):
        print(f"Preparing {item.contract_id}: {item.filename}")
        output, source_hash, output_hash, quotes = prepare_one(
            source_dir,
            output_dir,
            item,
            index,
            args.overwrite,
        )
        prepared.append((item, output, source_hash, output_hash, quotes))
        print(f"Ready {item.contract_id}: {output}")
    write_expected_csv(csv_path, prepared)
    expected_rows = len(SOURCES) * len(EXPECTED_CRITERIA)
    print(f"Expected cases: {expected_rows}")
    print(f"CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
