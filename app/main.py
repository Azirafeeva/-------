from contextlib import asynccontextmanager
import csv
import io
import json
from pathlib import Path
from typing import Optional, List, Dict, Any

import pandas as pd
from fastapi import FastAPI, Depends, Query, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload

from app.database import Base, engine, SessionLocal, get_db
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
from app.seed import seed_database
from app.importer import import_file_content, ImportValidationError

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="ФГБУ ГМЦ Росстата — Информационная система бухгалтерской отчетности (ГИР БО)",
    description="Прототип БД и аналитической платформы бухгалтерской (финансовой) отчетности организаций (12 таблиц, XML 5.03 / 5.08, Приказ Минфина №66н)",
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _matches_query(org: Organization, q: Optional[str]) -> bool:
    if not q:
        return True
    q_norm = q.strip().lower()
    if not q_norm:
        return True
    fields = [
        org.inn or "",
        org.ogrn or "",
        org.okpo or "",
        org.name_full or "",
        org.name_short or "",
    ]
    return any(q_norm in f.lower() for f in fields)


def _build_top_revenue_rows(
    db: Session,
    year: int = 2024,
    q: Optional[str] = None,
    region_id: Optional[int] = None,
    industry_id: Optional[int] = None,
    size_class: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = (
        db.query(Organization)
        .options(
            joinedload(Organization.region),
            joinedload(Organization.industry),
        )
    )
    if region_id is not None:
        query = query.filter(Organization.region_id == region_id)
    if industry_id is not None:
        # Поддержка фильтрации как по конечному ОКВЭД, так и по родительскому разделу
        child_ids = [
            i.id for i in db.query(Industry).filter(
                (Industry.id == industry_id) | (Industry.parent_id == industry_id)
            ).all()
        ]
        query = query.filter(Organization.okved_id.in_(child_ids if child_ids else [industry_id]))
    if size_class:
        query = query.filter(Organization.size_class == size_class)

    orgs = [o for o in query.all() if _matches_query(o, q)]
    if not orgs:
        return []

    org_ids = [o.id for o in orgs]
    orgs_by_id = {o.id: o for o in orgs}

    packages = (
        db.query(ReportPackage)
        .filter(
            ReportPackage.org_id.in_(org_ids),
            ReportPackage.report_year == year,
            ReportPackage.is_current == True,  # noqa: E712
        )
        .all()
    )
    if not packages:
        return []

    pkg_by_org = {p.org_id: p for p in packages}
    pkg_ids = [p.id for p in packages]

    reports = (
        db.query(Report)
        .options(joinedload(Report.unit))
        .filter(Report.package_id.in_(pkg_ids))
        .all()
    )
    rep_ids = [r.id for r in reports]
    rep_to_pkg = {r.id: r.package_id for r in reports}
    pkg_unit_map: Dict[int, tuple[str, float]] = {}
    for r in reports:
        u_code = r.unit_code or "384"
        to_rub = float(r.unit.to_rub) if r.unit and r.unit.to_rub else 1000.0
        pkg_unit_map[r.package_id] = (u_code, to_rub)

    target_codes = ["2110", "2120", "2400", "1600", "1100", "1200", "1300", "1520"]
    indicators = db.query(Indicator).filter(Indicator.code.in_(target_codes)).all()
    ind_id_to_code = {ind.id: ind.code for ind in indicators}

    values = (
        db.query(ReportValue)
        .filter(
            ReportValue.report_id.in_(rep_ids),
            ReportValue.indicator_id.in_(list(ind_id_to_code.keys())),
            ReportValue.is_comparative == False,  # noqa: E712
        )
        .all()
    )

    pkg_metrics: Dict[int, Dict[str, float]] = {pid: {} for pid in pkg_ids}
    for rv in values:
        pid = rep_to_pkg.get(rv.report_id)
        code = ind_id_to_code.get(rv.indicator_id)
        if pid and code and rv.value is not None:
            pkg_metrics[pid][code] = float(rv.value)

    results = []
    for org_id, pkg in pkg_by_org.items():
        org = orgs_by_id[org_id]
        m = pkg_metrics.get(pkg.id, {})
        unit_code, to_rub = pkg_unit_map.get(pkg.id, ("384", 1000.0))

        revenue = m.get("2110", 0.0)
        cost = m.get("2120", 0.0)
        net_profit = m.get("2400", 0.0)
        assets = m.get("1600", 0.0)
        equity = m.get("1300", 0.0)
        payables = m.get("1520", 0.0)

        profitability = round((net_profit / revenue) * 100.0, 2) if revenue > 0 else 0.0

        results.append(
            {
                "org_id": org.id,
                "inn": org.inn,
                "kpp": pkg.kpp,
                "ogrn": org.ogrn,
                "okpo": org.okpo,
                "name_full": org.name_full,
                "name_short": org.name_short or org.name_full,
                "region_id": org.region_id,
                "region_code": org.region.code if org.region else "",
                "region_name": org.region.name if org.region else "Не указан",
                "federal_district": org.region.federal_district if org.region else "",
                "industry_id": org.okved_id,
                "okved_code": org.industry.code if org.industry else "",
                "okved_name": org.industry.name if org.industry else "Не указан",
                "size_class": org.size_class,
                "report_year": pkg.report_year,
                "correction_no": pkg.correction_no,
                "format_version": pkg.format_version,
                "knd": pkg.knd,
                "generator": pkg.generator,
                "unit_code": unit_code,
                "to_rub": to_rub,
                "revenue": revenue,
                "cost_of_sales": cost,
                "net_profit": net_profit,
                "assets": assets,
                "equity": equity,
                "payables": payables,
                "profitability_pct": profitability,
                "revenue_rub": round(revenue * to_rub, 2),
                "net_profit_rub": round(net_profit * to_rub, 2),
                "assets_rub": round(assets * to_rub, 2),
            }
        )

    results.sort(key=lambda x: x["revenue"], reverse=True)
    return results


@app.get("/", response_class=HTMLResponse)
def root_ui():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>ФГБУ ГМЦ Росстата — UI не найден</h1>", status_code=404)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/dictionaries")
def get_dictionaries(db: Session = Depends(get_db)):
    regions = db.query(Region).order_by(Region.code).all()
    industries = db.query(Industry).order_by(Industry.code).all()
    forms = db.query(ReportForm).order_by(ReportForm.id).all()
    units = db.query(Unit).order_by(Unit.code).all()
    years_raw = db.query(ReportPackage.report_year).distinct().order_by(ReportPackage.report_year.desc()).all()
    years = [y[0] for y in years_raw] or [2025, 2024, 2023]
    sizes_raw = db.query(Organization.size_class).distinct().all()
    sizes = sorted({s[0] for s in sizes_raw if s[0]})

    return {
        "regions": [
            {"id": r.id, "code": r.code, "name": r.name, "federal_district": r.federal_district}
            for r in regions
        ],
        "industries": [
            {
                "id": ind.id,
                "code": ind.code,
                "name": ind.name,
                "parent_id": ind.parent_id,
                "section": ind.section,
            }
            for ind in industries
        ],
        "report_forms": [{"id": f.id, "code": f.code, "name": f.name} for f in forms],
        "units": [{"code": u.code, "name": u.name, "to_rub": float(u.to_rub)} for u in units],
        "years": years,
        "size_classes": sizes,
    }


@app.get("/api/organizations")
def list_organizations(
    q: Optional[str] = Query(None, description="Поиск по ИНН, ОГРН или наименованию"),
    region_id: Optional[int] = Query(None),
    industry_id: Optional[int] = Query(None),
    size_class: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(Organization).options(
        joinedload(Organization.region),
        joinedload(Organization.industry),
    )
    if region_id is not None:
        query = query.filter(Organization.region_id == region_id)
    if industry_id is not None:
        child_ids = [
            i.id for i in db.query(Industry).filter(
                (Industry.id == industry_id) | (Industry.parent_id == industry_id)
            ).all()
        ]
        query = query.filter(Organization.okved_id.in_(child_ids if child_ids else [industry_id]))
    if size_class:
        query = query.filter(Organization.size_class == size_class)
    if year is not None:
        org_ids_with_year = [
            r[0]
            for r in db.query(ReportPackage.org_id)
            .filter(ReportPackage.report_year == year, ReportPackage.is_current == True)  # noqa: E712
            .distinct()
            .all()
        ]
        query = query.filter(Organization.id.in_(org_ids_with_year))

    orgs = [o for o in query.order_by(Organization.id).all() if _matches_query(o, q)]
    return [
        {
            "id": o.id,
            "inn": o.inn,
            "ogrn": o.ogrn,
            "okpo": o.okpo,
            "name_full": o.name_full,
            "name_short": o.name_short or o.name_full,
            "okopf": o.okopf,
            "okfs": o.okfs,
            "size_class": o.size_class,
            "region_id": o.region_id,
            "region_code": o.region.code if o.region else "",
            "region_name": o.region.name if o.region else "",
            "industry_id": o.okved_id,
            "okved_code": o.industry.code if o.industry else "",
            "okved_name": o.industry.name if o.industry else "",
            "address_raw": o.address_raw,
        }
        for o in orgs
    ]


@app.get("/api/analytics/top-revenue")
def get_top_revenue(
    year: int = Query(2024, description="Отчетный год"),
    q: Optional[str] = Query(None),
    region_id: Optional[int] = Query(None),
    industry_id: Optional[int] = Query(None),
    size_class: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return _build_top_revenue_rows(
        db=db,
        year=year,
        q=q,
        region_id=region_id,
        industry_id=industry_id,
        size_class=size_class,
    )


@app.get("/api/analytics/summary")
def get_analytics_summary(
    year: int = Query(2024),
    q: Optional[str] = Query(None),
    region_id: Optional[int] = Query(None),
    industry_id: Optional[int] = Query(None),
    size_class: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    rows = _build_top_revenue_rows(
        db=db,
        year=year,
        q=q,
        region_id=region_id,
        industry_id=industry_id,
        size_class=size_class,
    )
    total_orgs_in_db = db.query(Organization).count()
    total_packages_in_db = db.query(ReportPackage).filter(ReportPackage.is_current == True).count()  # noqa: E712

    count = len(rows)
    total_revenue = sum(r["revenue"] for r in rows)
    total_net_profit = sum(r["net_profit"] for r in rows)
    total_assets = sum(r["assets"] for r in rows)
    profitable_count = sum(1 for r in rows if r["net_profit"] > 0)
    profitable_share = round((profitable_count / count) * 100.0, 1) if count > 0 else 0.0

    by_region: Dict[str, Dict[str, Any]] = {}
    by_industry: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        reg_key = f"{r['region_code']} — {r['region_name']}"
        if reg_key not in by_region:
            by_region[reg_key] = {"region": reg_key, "count": 0, "revenue": 0.0, "net_profit": 0.0}
        by_region[reg_key]["count"] += 1
        by_region[reg_key]["revenue"] += r["revenue"]
        by_region[reg_key]["net_profit"] += r["net_profit"]

        ind_key = f"{r['okved_code']} — {r['okved_name']}"
        if ind_key not in by_industry:
            by_industry[ind_key] = {"industry": ind_key, "code": r["okved_code"], "count": 0, "revenue": 0.0, "net_profit": 0.0}
        by_industry[ind_key]["count"] += 1
        by_industry[ind_key]["revenue"] += r["revenue"]
        by_industry[ind_key]["net_profit"] += r["net_profit"]

    return {
        "year": year,
        "organizations_count": count,
        "total_organizations_in_db": total_orgs_in_db,
        "total_packages_in_db": total_packages_in_db,
        "total_revenue": round(total_revenue, 2),
        "total_net_profit": round(total_net_profit, 2),
        "total_assets": round(total_assets, 2),
        "profitable_count": profitable_count,
        "profitable_share_pct": profitable_share,
        "by_region": sorted(by_region.values(), key=lambda x: x["revenue"], reverse=True),
        "by_industry": sorted(by_industry.values(), key=lambda x: x["revenue"], reverse=True),
    }


@app.get("/api/analytics/dynamics/{org_id}")
def get_organization_dynamics(org_id: int, db: Session = Depends(get_db)):
    org = (
        db.query(Organization)
        .options(joinedload(Organization.region), joinedload(Organization.industry))
        .filter(Organization.id == org_id)
        .first()
    )
    if not org:
        raise HTTPException(status_code=404, detail="Организация не найдена")

    packages = (
        db.query(ReportPackage)
        .filter(ReportPackage.org_id == org_id)
        .order_by(ReportPackage.report_year.asc(), ReportPackage.correction_no.desc())
        .all()
    )

    current_pkgs = [p for p in packages if p.is_current]
    pkg_ids = [p.id for p in current_pkgs]

    reports = (
        db.query(Report)
        .options(joinedload(Report.form), joinedload(Report.unit))
        .filter(Report.package_id.in_(pkg_ids))
        .all()
        if pkg_ids
        else []
    )
    rep_ids = [r.id for r in reports]
    rep_map = {r.id: r for r in reports}

    values = (
        db.query(ReportValue)
        .options(joinedload(ReportValue.indicator), joinedload(ReportValue.slice))
        .filter(ReportValue.report_id.in_(rep_ids), ReportValue.is_comparative == False)  # noqa: E712
        .all()
        if rep_ids
        else []
    )

    forms_data: Dict[str, Dict[str, Any]] = {}
    for f in db.query(ReportForm).order_by(ReportForm.id).all():
        forms_data[f.code] = {
            "form_code": f.code,
            "form_name": f.name,
            "rows": {},
        }

    years_set = sorted({p.report_year for p in current_pkgs})
    chart_series = {yr: {"2110": 0.0, "2120": 0.0, "2400": 0.0, "1600": 0.0} for yr in years_set}

    for rv in values:
        rep = rep_map[rv.report_id]
        f_code = rep.form.code if rep.form else "F1"
        ind = rv.indicator
        sl = rv.slice
        if not ind:
            continue

        row_key = f"{ind.code}__{sl.code if sl else 'total'}"
        if row_key not in forms_data[f_code]["rows"]:
            forms_data[f_code]["rows"][row_key] = {
                "indicator_code": ind.code,
                "indicator_name": ind.name,
                "is_total": ind.is_total,
                "is_aggregated": ind.is_aggregated,
                "slice_code": sl.code if sl else "total",
                "slice_name": sl.name if sl else "Итого",
                "values_by_year": {},
            }

        val_float = float(rv.value) if rv.value is not None else None
        forms_data[f_code]["rows"][row_key]["values_by_year"][str(rv.report_year)] = val_float

        if rv.report_year in chart_series and ind.code in chart_series[rv.report_year] and (not sl or sl.code == "total"):
            chart_series[rv.report_year][ind.code] = val_float or 0.0

    formatted_forms = {}
    for f_code, f_info in forms_data.items():
        rows_list = sorted(f_info["rows"].values(), key=lambda x: (x["indicator_code"], x["slice_code"]))
        formatted_forms[f_code] = {
            "form_code": f_code,
            "form_name": f_info["form_name"],
            "rows": rows_list,
        }

    latest_pkg = current_pkgs[-1] if current_pkgs else None

    return {
        "organization": {
            "id": org.id,
            "inn": org.inn,
            "ogrn": org.ogrn,
            "okpo": org.okpo,
            "name_full": org.name_full,
            "name_short": org.name_short or org.name_full,
            "okopf": org.okopf,
            "okfs": org.okfs,
            "size_class": org.size_class,
            "size_source": org.size_source,
            "region_code": org.region.code if org.region else "",
            "region_name": org.region.name if org.region else "",
            "federal_district": org.region.federal_district if org.region else "",
            "okved_code": org.industry.code if org.industry else "",
            "okved_name": org.industry.name if org.industry else "",
            "address_raw": org.address_raw,
            "address_norm": org.address_norm,
            "signer_type": org.signer_type,
            "latest_package": {
                "kpp": latest_pkg.kpp,
                "report_year": latest_pkg.report_year,
                "knd": latest_pkg.knd,
                "format_version": latest_pkg.format_version,
                "correction_no": latest_pkg.correction_no,
                "generator": latest_pkg.generator,
                "doc_date": str(latest_pkg.doc_date) if latest_pkg.doc_date else None,
                "audit_flag": latest_pkg.audit_flag,
                "signer": latest_pkg.signer,
            }
            if latest_pkg
            else None,
        },
        "years": years_set,
        "dynamics_chart": [
            {
                "year": yr,
                "revenue_2110": chart_series[yr]["2110"],
                "cost_2120": chart_series[yr]["2120"],
                "net_profit_2400": chart_series[yr]["2400"],
                "assets_1600": chart_series[yr]["1600"],
            }
            for yr in years_set
        ],
        "forms": formatted_forms,
        "packages_history": [
            {
                "id": p.id,
                "report_year": p.report_year,
                "kpp": p.kpp,
                "knd": p.knd,
                "format_version": p.format_version,
                "correction_no": p.correction_no,
                "generator": p.generator,
                "is_current": p.is_current,
                "doc_date": str(p.doc_date) if p.doc_date else None,
            }
            for p in packages
        ],
    }


@app.get("/api/analytics/compare")
def compare_organizations(
    org1_id: int = Query(..., description="ID первой организации"),
    org2_id: int = Query(..., description="ID второй организации"),
    year: int = Query(2024, description="Год для сопоставления"),
    db: Session = Depends(get_db),
):
    d1 = get_organization_dynamics(org1_id, db)
    d2 = get_organization_dynamics(org2_id, db)

    compare_codes = [
        ("2110", "Выручка", "F2"),
        ("2120", "Себестоимость продаж", "F2"),
        ("2100", "Валовая прибыль (убыток)", "F2"),
        ("2200", "Прибыль (убыток) от продаж", "F2"),
        ("2400", "Чистая прибыль (убыток)", "F2"),
        ("1100", "Внеоборотные активы", "F1"),
        ("1200", "Оборотные активы", "F1"),
        ("1210", "Запасы", "F1"),
        ("1230", "Дебиторская задолженность", "F1"),
        ("1250", "Денежные средства", "F1"),
        ("1600", "Баланс (Актив)", "F1"),
        ("1300", "Капитал и резервы", "F1"),
        ("1500", "Краткосрочные обязательства", "F1"),
        ("1520", "Кредиторская задолженность", "F1"),
    ]

    def _extract_val(dyn_obj: Dict[str, Any], form_code: str, ind_code: str, yr: int) -> float:
        rows = dyn_obj.get("forms", {}).get(form_code, {}).get("rows", [])
        for r in rows:
            if r["indicator_code"] == ind_code and r["slice_code"] == "total":
                v = r["values_by_year"].get(str(yr))
                return float(v) if v is not None else 0.0
        return 0.0

    comparison_rows = []
    for code, name, f_code in compare_codes:
        v1 = _extract_val(d1, f_code, code, year)
        v2 = _extract_val(d2, f_code, code, year)
        diff = round(v1 - v2, 2)
        ratio_pct = round(((v1 - v2) / abs(v2)) * 100.0, 1) if v2 != 0 else None
        comparison_rows.append(
            {
                "code": code,
                "name": name,
                "form_code": f_code,
                "org1_value": v1,
                "org2_value": v2,
                "diff": diff,
                "diff_pct": ratio_pct,
            }
        )

    return {
        "year": year,
        "org1": d1["organization"],
        "org2": d2["organization"],
        "metrics": comparison_rows,
    }


@app.post("/api/import")
async def upload_and_import_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Загружен пустой файл")
    try:
        result = import_file_content(db=db, file_name=file.filename or "", content=content)
        return result
    except ImportValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/export")
def export_data(
    format: str = Query("csv", description="Формат выгрузки: csv, xlsx или json"),
    year: int = Query(2024),
    q: Optional[str] = Query(None),
    region_id: Optional[int] = Query(None),
    industry_id: Optional[int] = Query(None),
    size_class: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    fmt = format.lower().strip()
    if fmt not in ("csv", "xlsx", "json"):
        raise HTTPException(status_code=400, detail="Поддерживаются только форматы: csv, xlsx, json")

    rows = _build_top_revenue_rows(
        db=db,
        year=year,
        q=q,
        region_id=region_id,
        industry_id=industry_id,
        size_class=size_class,
    )

    if fmt == "json":
        json_bytes = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
        return StreamingResponse(
            io.BytesIO(json_bytes),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="rosstat_export_{year}.json"'},
        )

    headers_ru = [
        "ИНН",
        "КПП",
        "ОГРН",
        "Наименование организации",
        "Регион РФ",
        "Код ОКВЭД2",
        "Отрасль ОКВЭД2",
        "Категория субъекта",
        "Отчетный год",
        "Версия XML",
        "Выручка (2110), тыс. руб.",
        "Чистая прибыль (2400), тыс. руб.",
        "Баланс Актив (1600), тыс. руб.",
        "Капитал (1300), тыс. руб.",
        "Рентабельность, %",
        "Выручка, руб.",
    ]

    table_data = [
        [
            r["inn"],
            r["kpp"],
            r["ogrn"] or "",
            r["name_short"],
            r["region_name"],
            r["okved_code"],
            r["okved_name"],
            r["size_class"],
            r["report_year"],
            r["format_version"],
            r["revenue"],
            r["net_profit"],
            r["assets"],
            r["equity"],
            r["profitability_pct"],
            r["revenue_rub"],
        ]
        for r in rows
    ]

    if fmt == "csv":
        s_buf = io.StringIO()
        writer = csv.writer(s_buf, delimiter=";", lineterminator="\r\n")
        writer.writerow(headers_ru)
        writer.writerows(table_data)
        csv_bytes = s_buf.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(csv_bytes),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="rosstat_export_{year}.csv"'},
        )

    # fmt == "xlsx"
    df = pd.DataFrame(table_data, columns=headers_ru)
    x_buf = io.BytesIO()
    with pd.ExcelWriter(x_buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=f"Отчетность_{year}")
    x_buf.seek(0)
    return StreamingResponse(
        x_buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="rosstat_export_{year}.xlsx"'},
    )


@app.get("/api/import-logs")
def get_import_logs(db: Session = Depends(get_db)):
    logs = db.query(ImportFile).order_by(ImportFile.id.desc()).limit(100).all()
    return [
        {
            "id": item.id,
            "sha256": item.sha256,
            "file_name": item.file_name,
            "source_encoding": item.source_encoding,
            "format_version": item.format_version,
            "storage_uri": item.storage_uri,
            "package_id": item.package_id,
            "status": item.status,
            "error": item.error,
            "received_at": item.received_at.isoformat() if item.received_at else None,
        }
        for item in logs
    ]
