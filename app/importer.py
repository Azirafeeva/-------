import hashlib
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional, List

import pandas as pd
from lxml import etree
from sqlalchemy.orm import Session

from app.models import (
    Region,
    Industry,
    Unit,
    ReportForm,
    Indicator,
    IndicatorSource,
    ValueSlice,
    Organization,
    ReportPackage,
    Report,
    ReportValue,
    ImportFile,
)


class ImportValidationError(ValueError):
    """Raised when an uploaded file has an invalid format or structure."""
    pass


def _parse_date(date_str: Optional[str], fallback_year: int) -> date:
    if not date_str:
        return date(fallback_year, 12, 31)
    date_str = str(date_str).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return date(fallback_year, 12, 31)


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    s = str(val).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if s in ("", "-", "—", "None", "null", "nan"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _resolve_region(db: Session, inn: str, region_code: Optional[str] = None) -> Optional[Region]:
    code = (region_code or "").strip()
    if not code and inn and len(inn) >= 2:
        code = inn[:2]
    if not code:
        return None
    reg = db.query(Region).filter(Region.code == code).first()
    if not reg:
        reg = Region(code=code, name=f"Субъект РФ (код {code})", federal_district="РФ")
        db.add(reg)
        db.flush()
    return reg


def _resolve_industry(db: Session, okved_code: Optional[str], okved_name: Optional[str] = None) -> Optional[Industry]:
    if not okved_code:
        return None
    code = str(okved_code).strip()
    ind = db.query(Industry).filter(Industry.code == code).first()
    if not ind:
        ind = Industry(
            code=code,
            name=okved_name or f"Вид деятельности ОКВЭД2 {code}",
            section="K" if code.startswith("64") else "M",
        )
        db.add(ind)
        db.flush()
    return ind


def _ensure_unit(db: Session, unit_code: str) -> Unit:
    unit_code = (unit_code or "384").strip()
    unit = db.query(Unit).filter(Unit.code == unit_code).first()
    if not unit:
        to_rub_map = {"383": Decimal("1"), "384": Decimal("1000"), "385": Decimal("1000000")}
        name_map = {"383": "Рубль", "384": "Тысяча рублей", "385": "Миллион рублей"}
        unit = Unit(
            code=unit_code,
            name=name_map.get(unit_code, f"ОКЕИ {unit_code}"),
            to_rub=to_rub_map.get(unit_code, Decimal("1000")),
        )
        db.add(unit)
        db.flush()
    return unit


def _get_default_slice(db: Session) -> ValueSlice:
    sl = db.query(ValueSlice).filter(ValueSlice.code == "total").first()
    if not sl:
        sl = ValueSlice(code="total", name="Итого (скалярное значение)", form_id=None)
        db.add(sl)
        db.flush()
    return sl


def _add_report_value(
    db: Session,
    report_id: int,
    indicator_id: int,
    slice_id: int,
    as_of: date,
    report_year: int,
    raw_val: Any,
    source_column: str,
    is_comparative: bool,
) -> ReportValue:
    dec_val = _parse_decimal(raw_val)
    if dec_val is None:
        status = "not_disclosed"
        val_to_store = None
    elif dec_val == Decimal("0"):
        status = "zero"
        val_to_store = Decimal("0")
    else:
        status = "reported"
        val_to_store = dec_val

    rv = ReportValue(
        report_id=report_id,
        indicator_id=indicator_id,
        slice_id=slice_id,
        as_of=as_of,
        report_year=report_year,
        value=val_to_store,
        status=status,
        source_column=source_column,
        is_comparative=is_comparative,
    )
    db.add(rv)
    return rv


def _supersede_previous_packages(db: Session, org_id: int, report_year: int, new_package: ReportPackage) -> None:
    prev_packages = (
        db.query(ReportPackage)
        .filter(
            ReportPackage.org_id == org_id,
            ReportPackage.report_year == report_year,
            ReportPackage.is_current == True,  # noqa: E712
            ReportPackage.id != new_package.id,
        )
        .all()
    )
    for prev in prev_packages:
        prev.is_current = False
        prev.superseded_by = new_package.id


def import_file_content(
    db: Session,
    file_name: str,
    content: bytes,
) -> Dict[str, Any]:
    """
    Универсальная точка входа для импорта файла (.xml, .xlsx, .json).
    Вычисляет SHA-256, проверяет идемпотентность в import_files и парсит данные.
    """
    if not file_name:
        raise ImportValidationError("Имя файла не указано")

    ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
    if ext not in ("xml", "xlsx", "json"):
        raise ImportValidationError(
            f"Неподдерживаемый формат файла '.{ext}'. Разрешены только .xml, .xlsx, .json"
        )

    sha256_hash = hashlib.sha256(content).hexdigest()

    existing_import = db.query(ImportFile).filter(ImportFile.sha256 == sha256_hash).first()
    if existing_import and existing_import.status == "success":
        pkg = existing_import.package
        org = pkg.organization if pkg else None
        return {
            "status": "duplicate",
            "idempotent": True,
            "duplicate_prevented": True,
            "message": "Файл с такой контрольной суммой SHA-256 уже был успешно импортирован ранее",
            "sha256": sha256_hash,
            "import_file_id": existing_import.id,
            "package_id": existing_import.package_id,
            "organization_id": org.id if org else None,
            "inn": org.inn if org else None,
            "name_full": org.name_full if org else None,
        }

    if not existing_import:
        import_record = ImportFile(
            sha256=sha256_hash,
            file_name=file_name,
            storage_uri=f"local://imports/{sha256_hash}_{file_name}",
            status="pending",
        )
        db.add(import_record)
        db.flush()
    else:
        import_record = existing_import
        import_record.file_name = file_name
        import_record.status = "pending"
        import_record.error = None
        db.flush()

    try:
        if ext == "xml":
            result = _import_xml_fns(db, content, import_record)
        elif ext == "json":
            result = _import_json_report(db, content, import_record)
        elif ext == "xlsx":
            result = _import_xlsx_report(db, content, import_record)
        else:
            raise ImportValidationError("Неподдерживаемый формат")

        import_record.status = "success"
        import_record.package_id = result["package_id"]
        db.commit()
        return {
            "status": "success",
            "idempotent": False,
            "duplicate_prevented": False,
            "message": "Отчетность успешно загружена и сохранена в БД",
            "sha256": sha256_hash,
            "import_file_id": import_record.id,
            **result,
        }
    except Exception as exc:
        db.rollback()
        # Записываем ошибку в журнал импорта в новой транзакции
        err_rec = db.query(ImportFile).filter(ImportFile.sha256 == sha256_hash).first()
        if not err_rec:
            err_rec = ImportFile(
                sha256=sha256_hash,
                file_name=file_name,
                storage_uri=f"local://imports/{sha256_hash}_{file_name}",
                status="error",
                error={"detail": str(exc)},
            )
            db.add(err_rec)
        else:
            err_rec.status = "error"
            err_rec.error = {"detail": str(exc)}
        db.commit()
        if isinstance(exc, ImportValidationError):
            raise
        raise ImportValidationError(f"Ошибка разбора файла {file_name}: {exc}") from exc


def _decode_xml_bytes(content: bytes) -> tuple[str, str]:
    """Декодирует XML из UTF-8 или Windows-1251."""
    head = content[:250].decode("ascii", errors="ignore").lower()
    if "windows-1251" in head or "cp1251" in head:
        try:
            return content.decode("cp1251"), "windows-1251"
        except UnicodeDecodeError:
            pass
    try:
        return content.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return content.decode("cp1251", errors="replace"), "windows-1251"


def _import_xml_fns(db: Session, content: bytes, import_record: ImportFile) -> Dict[str, Any]:
    raw_text, encoding = _decode_xml_bytes(content)
    import_record.source_encoding = encoding
    import_record.raw_xml = raw_text

    try:
        parser = etree.XMLParser(recover=False, remove_blank_text=True)
        root = etree.fromstring(content, parser=parser)
    except Exception as e:
        raise ImportValidationError(f"Некорректный XML-документ: {e}") from e

    if root.tag != "Файл":
        doc_node = root if root.tag == "Документ" else root.find(".//Документ")
        if doc_node is None:
            raise ImportValidationError("Корневой элемент XML не соответствует формату ФНС (<Файл>/<Документ>)")
    else:
        doc_node = root.find("Документ")
        if doc_node is None:
            raise ImportValidationError("В XML отсутствует узел <Документ>")

    format_version = (root.get("ВерсФорм") or doc_node.get("ВерсФорм") or "5.08").strip()
    if format_version not in ("5.03", "5.08"):
        format_version = "5.03" if format_version.startswith("5.03") else "5.08"

    generator = root.get("ВерсПрог") or doc_node.get("ВерсПрог") or "XML Importer"
    import_record.format_version = format_version

    knd = (doc_node.get("КНД") or ("0710096" if format_version == "5.03" else "0710099")).strip()
    report_year = int(doc_node.get("ОтчетГод") or datetime.now().year - 1)
    period_code = (doc_node.get("Период") or "34").strip()
    correction_no = int(doc_node.get("НомКорр") or 0)
    doc_date = _parse_date(doc_node.get("ДатаДок"), report_year + 1)
    unit_code = (doc_node.get("ОКЕИ") or "384").strip()
    audit_flag = str(doc_node.get("ПризнАудит") or "0") in ("1", "true", "True", "Да")

    svnp_node = doc_node.find("СвНП")
    npul_node = svnp_node.find("НПЮЛ") if svnp_node is not None else doc_node.find(".//НПЮЛ")
    if npul_node is None and svnp_node is not None:
        npul_node = svnp_node

    if npul_node is None:
        raise ImportValidationError("В XML отсутствует блок сведений о налогоплательщике (<СвНП>/<НПЮЛ>)")

    inn = (npul_node.get("ИННЮЛ") or npul_node.get("ИНН") or "").strip()
    if not inn:
        raise ImportValidationError("Не найден атрибут ИННЮЛ в блоке сведений об организации")

    kpp = (npul_node.get("КПП") or "").strip()
    name_full = (npul_node.get("НаимОрг") or f"Организация ИНН {inn}").strip()
    name_short = (npul_node.get("НаимОргСокр") or name_full).strip()
    ogrn = (npul_node.get("ОГРН") or doc_node.get("ОГРН") or "").strip() or None
    okpo = (npul_node.get("ОКПО") or doc_node.get("ОКПО") or (svnp_node.get("ОКПО") if svnp_node is not None else "") or "").strip() or None
    okopf = (npul_node.get("ОКОПФ") or doc_node.get("ОКОПФ") or (svnp_node.get("ОКОПФ") if svnp_node is not None else "") or "12300").strip()
    okfs = (npul_node.get("ОКФС") or doc_node.get("ОКФС") or (svnp_node.get("ОКФС") if svnp_node is not None else "") or "16").strip()
    okved_code = (npul_node.get("ОКВЭД2") or doc_node.get("ОКВЭД2") or (svnp_node.get("ОКВЭД2") if svnp_node is not None else "") or "70.22").strip()
    size_class = (npul_node.get("КатСуб") or doc_node.get("КатСуб") or ("Малое" if knd == "0710096" else "Среднее")).strip()

    # Адрес
    adr_node = npul_node.find("АдрМН")
    if adr_node is None:
        adr_node = doc_node.find(".//АдрМН")
    region_code = inn[:2]
    address_raw = npul_node.get("АдрМН") or ""
    address_norm = {"region_code": region_code}
    if adr_node is not None:
        region_code = (adr_node.get("КодРегион") or region_code).strip()
        city = adr_node.get("Город") or adr_node.get("НаселПункт") or ""
        street = adr_node.get("Улица") or ""
        house = adr_node.get("Дом") or ""
        postal = adr_node.get("Индекс") or ""
        parts = [p for p in [postal, f"рег. {region_code}", city, street, house] if p]
        address_raw = ", ".join(parts) if parts else address_raw
        address_norm = {
            "postal_code": postal,
            "region_code": region_code,
            "city": city,
            "street": street,
            "house": house,
        }

    # Подписант
    podpisant_node = doc_node.find("Подписант")
    signer_info = {"role": "Руководитель", "full_name": "Не указан"}
    signer_type = "director"
    if podpisant_node is not None:
        pr_podp = podpisant_node.get("ПрПодп") or "1"
        signer_type = "director" if pr_podp == "1" else "representative"
        fio_node = podpisant_node.find("ФИО")
        if fio_node is not None:
            fam = fio_node.get("Фамилия") or ""
            im = fio_node.get("Имя") or ""
            otch = fio_node.get("Отчество") or ""
            full_fio = " ".join(x for x in [fam, im, otch] if x)
        else:
            full_fio = podpisant_node.get("ФИО") or "Подписант ЮЛ"
        signer_info = {
            "pr_podp": pr_podp,
            "signer_type": signer_type,
            "full_name": full_fio,
        }

    region = _resolve_region(db, inn, region_code)
    industry = _resolve_industry(db, okved_code)
    _ensure_unit(db, unit_code)
    default_slice = _get_default_slice(db)

    org = db.query(Organization).filter(Organization.inn == inn).first()
    if not org:
        org = Organization(
            inn=inn,
            ogrn=ogrn,
            okpo=okpo,
            name_full=name_full,
            name_short=name_short,
            okved_id=industry.id if industry else None,
            okopf=okopf,
            okfs=okfs,
            region_id=region.id if region else None,
            region_source="xml_address",
            size_class=size_class,
            size_source="knd_form",
            address_raw=address_raw or f"Регион {region_code}",
            address_norm=address_norm,
            signer_type=signer_type,
        )
        db.add(org)
        db.flush()
    else:
        org.name_full = name_full
        org.name_short = name_short or org.name_short
        if ogrn:
            org.ogrn = ogrn
        if okpo:
            org.okpo = okpo
        if industry:
            org.okved_id = industry.id
        if region:
            org.region_id = region.id
        if address_raw:
            org.address_raw = address_raw
            org.address_norm = address_norm
        db.flush()

    pkg = ReportPackage(
        org_id=org.id,
        kpp=kpp,
        report_year=report_year,
        period_code=period_code,
        period_end=date(report_year, 12, 31),
        correction_no=correction_no,
        knd=knd,
        format_version=format_version,
        generator=generator,
        doc_date=doc_date,
        audit_flag=audit_flag,
        signer=signer_info,
        is_current=True,
    )
    db.add(pkg)
    db.flush()

    _supersede_previous_packages(db, org.id, report_year, pkg)

    # Загружаем маппинги XPath из таблицы indicator_sources для данной версии формата
    sources: List[IndicatorSource] = (
        db.query(IndicatorSource)
        .filter(IndicatorSource.format_version == format_version)
        .all()
    )

    forms_by_code: Dict[str, ReportForm] = {
        f.code: f for f in db.query(ReportForm).all()
    }
    reports_by_form: Dict[str, Report] = {}
    okud_map = {
        "F1": "0710001",
        "F2": "0710002",
        "F3": "0710004",
        "F4": "0710005",
        "F5": "0710006",
    }

    extracted_count = 0
    for src in sources:
        # Ищем узел по XPath относительно корня или документа
        xpath_expr = src.xml_path
        nodes = root.xpath(xpath_expr)
        if not nodes and xpath_expr.startswith("/Документ/"):
            rel_expr = "." + xpath_expr[len("/Документ"):]
            nodes = doc_node.xpath(rel_expr)

        if not nodes:
            continue

        node = nodes[0]
        form_code = src.form_code
        if form_code not in reports_by_form:
            form_obj = forms_by_code.get(form_code)
            if not form_obj:
                continue
            rep = Report(
                package_id=pkg.id,
                form_id=form_obj.id,
                section_code=form_code,
                okud=okud_map.get(form_code, "0710001"),
                unit_code=unit_code,
                source_path=f"/Файл/Документ/{form_code}",
            )
            db.add(rep)
            db.flush()
            reports_by_form[form_code] = rep

        rep = reports_by_form[form_code]

        # 1) Отчетный период: СумОтч
        val_otch = node.get("СумОтч")
        if val_otch is not None:
            _add_report_value(
                db=db,
                report_id=rep.id,
                indicator_id=src.indicator_id,
                slice_id=default_slice.id,
                as_of=date(report_year, 12, 31),
                report_year=report_year,
                raw_val=val_otch,
                source_column="СумОтч",
                is_comparative=False,
            )
            extracted_count += 1

        # 2) Предыдущий год: СумПрдщ или СумПред
        val_prev = node.get("СумПрдщ") if node.get("СумПрдщ") is not None else node.get("СумПред")
        col_prev = "СумПрдщ" if node.get("СумПрдщ") is not None else "СумПред"
        if val_prev is not None:
            _add_report_value(
                db=db,
                report_id=rep.id,
                indicator_id=src.indicator_id,
                slice_id=default_slice.id,
                as_of=date(report_year - 1, 12, 31),
                report_year=report_year - 1,
                raw_val=val_prev,
                source_column=col_prev,
                is_comparative=True,
            )
            extracted_count += 1

        # 3) Позапрошлый год: СумПрдшв
        val_prev2 = node.get("СумПрдшв")
        if val_prev2 is not None:
            _add_report_value(
                db=db,
                report_id=rep.id,
                indicator_id=src.indicator_id,
                slice_id=default_slice.id,
                as_of=date(report_year - 2, 12, 31),
                report_year=report_year - 2,
                raw_val=val_prev2,
                source_column="СумПрдшв",
                is_comparative=True,
            )
            extracted_count += 1

    db.flush()
    return {
        "package_id": pkg.id,
        "organization_id": org.id,
        "inn": org.inn,
        "name_full": org.name_full,
        "report_year": report_year,
        "format_version": format_version,
        "extracted_values": extracted_count,
    }


def _import_json_report(db: Session, content: bytes, import_record: ImportFile) -> Dict[str, Any]:
    try:
        text = content.decode("utf-8-sig")
        payload = json.loads(text)
    except Exception as e:
        raise ImportValidationError(f"Некорректный JSON файл: {e}") from e

    import_record.source_encoding = "UTF-8"
    import_record.format_version = str(payload.get("format_version") or "5.08")

    org_data = payload.get("organization") or payload
    inn = str(org_data.get("inn") or "").strip()
    if not inn:
        raise ImportValidationError("В JSON отсутствует поле 'inn' организации")

    name_full = str(org_data.get("name_full") or org_data.get("name") or f"ООО Организация {inn}").strip()
    name_short = str(org_data.get("name_short") or name_full).strip()
    ogrn = str(org_data.get("ogrn") or "").strip() or None
    okpo = str(org_data.get("okpo") or "").strip() or None
    kpp = str(org_data.get("kpp") or f"{inn[:4]}01001").strip()
    region_code = str(org_data.get("region_code") or inn[:2]).strip()
    okved_code = str(org_data.get("okved") or "62.01").strip()
    size_class = str(org_data.get("size_class") or "Малое").strip()
    report_year = int(payload.get("report_year") or 2024)
    correction_no = int(payload.get("correction_no") or 0)
    unit_code = str(payload.get("unit_code") or "384").strip()

    region = _resolve_region(db, inn, region_code)
    industry = _resolve_industry(db, okved_code)
    _ensure_unit(db, unit_code)
    default_slice = _get_default_slice(db)

    org = db.query(Organization).filter(Organization.inn == inn).first()
    if not org:
        org = Organization(
            inn=inn,
            ogrn=ogrn,
            okpo=okpo,
            name_full=name_full,
            name_short=name_short,
            okved_id=industry.id if industry else None,
            okopf=str(org_data.get("okopf") or "12300"),
            okfs=str(org_data.get("okfs") or "16"),
            region_id=region.id if region else None,
            region_source="json_import",
            size_class=size_class,
            size_source="json_import",
            address_raw=org_data.get("address") or f"Регион {region_code}",
            address_norm={"region_code": region_code},
            signer_type="director",
        )
        db.add(org)
        db.flush()

    pkg = ReportPackage(
        org_id=org.id,
        kpp=kpp,
        report_year=report_year,
        period_code="34",
        period_end=date(report_year, 12, 31),
        correction_no=correction_no,
        knd=str(payload.get("knd") or "0710099"),
        format_version=import_record.format_version,
        generator=str(payload.get("generator") or "JSON Data Importer"),
        doc_date=date(report_year + 1, 3, 25),
        audit_flag=bool(payload.get("audit_flag", False)),
        signer=payload.get("signer") or {"role": "Руководитель", "full_name": "Директор ЮЛ"},
        is_current=True,
    )
    db.add(pkg)
    db.flush()

    _supersede_previous_packages(db, org.id, report_year, pkg)

    indicators_by_code: Dict[str, Indicator] = {
        ind.code: ind for ind in db.query(Indicator).all()
    }
    forms_by_id: Dict[int, ReportForm] = {
        f.id: f for f in db.query(ReportForm).all()
    }
    reports_by_form_id: Dict[int, Report] = {}

    values_dict = payload.get("values") or payload.get("indicators") or {}
    extracted_count = 0

    if isinstance(values_dict, dict):
        items = [{"code": k, "value": v} for k, v in values_dict.items()]
    elif isinstance(values_dict, list):
        items = values_dict
    else:
        items = []

    for item in items:
        code = str(item.get("code") or "").strip()
        ind = indicators_by_code.get(code)
        if not ind:
            continue
        if ind.form_id not in reports_by_form_id:
            form_obj = forms_by_id.get(ind.form_id)
            rep = Report(
                package_id=pkg.id,
                form_id=ind.form_id,
                section_code=form_obj.code if form_obj else "F1",
                okud="0710001" if (form_obj and form_obj.code == "F1") else "0710002",
                unit_code=unit_code,
                source_path="json://values",
            )
            db.add(rep)
            db.flush()
            reports_by_form_id[ind.form_id] = rep

        rep = reports_by_form_id[ind.form_id]
        _add_report_value(
            db=db,
            report_id=rep.id,
            indicator_id=ind.id,
            slice_id=default_slice.id,
            as_of=date(report_year, 12, 31),
            report_year=report_year,
            raw_val=item.get("value"),
            source_column="СумОтч",
            is_comparative=False,
        )
        extracted_count += 1

    db.flush()
    return {
        "package_id": pkg.id,
        "organization_id": org.id,
        "inn": org.inn,
        "name_full": org.name_full,
        "report_year": report_year,
        "format_version": import_record.format_version,
        "extracted_values": extracted_count,
    }


def _import_xlsx_report(db: Session, content: bytes, import_record: ImportFile) -> Dict[str, Any]:
    try:
        df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise ImportValidationError(f"Не удалось прочитать Excel файл: {e}") from e

    if df.empty:
        raise ImportValidationError("Файл Excel пуст")

    import_record.source_encoding = "XLSX-Binary"
    import_record.format_version = "5.08"

    cols = {str(c).strip().lower(): c for c in df.columns}
    first_row = df.iloc[0]

    inn_col = cols.get("inn") or cols.get("инн")
    if not inn_col:
        raise ImportValidationError("В Excel файле отсутствует колонка 'inn' / 'ИНН'")

    inn = str(first_row[inn_col]).split(".")[0].strip()
    name_col = cols.get("name_full") or cols.get("name") or cols.get("наименование")
    name_full = str(first_row[name_col]).strip() if name_col else f"ООО Организация {inn}"
    year_col = cols.get("report_year") or cols.get("year") or cols.get("год")
    report_year = int(first_row[year_col]) if year_col and pd.notna(first_row[year_col]) else 2024
    okved_col = cols.get("okved") or cols.get("оквэд")
    okved_code = str(first_row[okved_col]).strip() if okved_col and pd.notna(first_row[okved_col]) else "62.01"
    region_col = cols.get("region_code") or cols.get("регион")
    region_code = str(first_row[region_col]).split(".")[0].zfill(2) if region_col and pd.notna(first_row[region_col]) else inn[:2]
    size_col = cols.get("size_class") or cols.get("размер")
    size_class = str(first_row[size_col]).strip() if size_col and pd.notna(first_row[size_col]) else "Среднее"

    region = _resolve_region(db, inn, region_code)
    industry = _resolve_industry(db, okved_code)
    _ensure_unit(db, "384")
    default_slice = _get_default_slice(db)

    org = db.query(Organization).filter(Organization.inn == inn).first()
    if not org:
        org = Organization(
            inn=inn,
            ogrn=f"102{inn[:2]}00{inn[-6:]}",
            okpo=inn[:8],
            name_full=name_full,
            name_short=name_full,
            okved_id=industry.id if industry else None,
            okopf="12300",
            okfs="16",
            region_id=region.id if region else None,
            region_source="xlsx_import",
            size_class=size_class,
            size_source="xlsx_import",
            address_raw=f"Субъект РФ {region_code}",
            address_norm={"region_code": region_code},
            signer_type="director",
        )
        db.add(org)
        db.flush()

    pkg = ReportPackage(
        org_id=org.id,
        kpp=f"{inn[:4]}01001",
        report_year=report_year,
        period_code="34",
        period_end=date(report_year, 12, 31),
        correction_no=0,
        knd="0710099",
        format_version="5.08",
        generator="Excel Template Loader",
        doc_date=date(report_year + 1, 3, 28),
        audit_flag=False,
        signer={"role": "Руководитель", "full_name": "Директор ЮЛ (XLSX)"},
        is_current=True,
    )
    db.add(pkg)
    db.flush()

    _supersede_previous_packages(db, org.id, report_year, pkg)

    indicators_by_code: Dict[str, Indicator] = {
        ind.code: ind for ind in db.query(Indicator).all()
    }
    forms_by_id: Dict[int, ReportForm] = {
        f.id: f for f in db.query(ReportForm).all()
    }
    reports_by_form_id: Dict[int, Report] = {}

    code_col = cols.get("indicator_code") or cols.get("code") or cols.get("код")
    val_col = cols.get("value") or cols.get("значение") or cols.get("сумма")

    extracted_count = 0
    if code_col and val_col:
        for _, row in df.iterrows():
            c_val = str(row[code_col]).split(".")[0].strip()
            ind = indicators_by_code.get(c_val)
            if not ind:
                continue
            if ind.form_id not in reports_by_form_id:
                form_obj = forms_by_id.get(ind.form_id)
                rep = Report(
                    package_id=pkg.id,
                    form_id=ind.form_id,
                    section_code=form_obj.code if form_obj else "F1",
                    okud="0710001" if (form_obj and form_obj.code == "F1") else "0710002",
                    unit_code="384",
                    source_path="xlsx://sheet1",
                )
                db.add(rep)
                db.flush()
                reports_by_form_id[ind.form_id] = rep

            rep = reports_by_form_id[ind.form_id]
            _add_report_value(
                db=db,
                report_id=rep.id,
                indicator_id=ind.id,
                slice_id=default_slice.id,
                as_of=date(report_year, 12, 31),
                report_year=report_year,
                raw_val=row[val_col],
                source_column="СумОтч",
                is_comparative=False,
            )
            extracted_count += 1

    db.flush()
    return {
        "package_id": pkg.id,
        "organization_id": org.id,
        "inn": org.inn,
        "name_full": org.name_full,
        "report_year": report_year,
        "format_version": "5.08",
        "extracted_values": extracted_count,
    }
