import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal, Base, engine
from app.seed import seed_database, generate_sample_files


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_database(db)
        generate_sample_files()
    finally:
        db.close()
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_01_root_ui_status(client: TestClient):
    """GET / отдает 200 OK и HTML-страницу дашборда ФГБУ ГМЦ Росстата."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "ФГБУ ГМЦ Росстата" in response.text


def test_02_search_by_existing_inn(client: TestClient):
    """Поиск по ИНН '2918010863' находит ООО 'Легкозайм плюс'."""
    response = client.get("/api/organizations", params={"q": "2918010863"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["inn"] == "2918010863"
    assert "ле" in data[0]["name_short"].lower()


def test_03_search_by_nonexistent_inn(client: TestClient):
    """Поиск по несуществующему ИНН '0000000000' возвращает пустой список."""
    response = client.get("/api/organizations", params={"q": "0000000000"})
    assert response.status_code == 200
    data = response.json()
    assert data == []


def test_04_filter_by_region(client: TestClient):
    """Фильтрация организаций по региону (Архангельская область, код '29')."""
    dict_res = client.get("/api/dictionaries")
    assert dict_res.status_code == 200
    regions = dict_res.json()["regions"]
    arkh_region = next(r for r in regions if r["code"] == "29")

    response = client.get("/api/organizations", params={"region_id": arkh_region["id"]})
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
    assert all(item["region_code"] == "29" for item in data)


def test_05_filter_by_industry_and_year(client: TestClient):
    """Фильтрация по отрасли ОКВЭД2 (70.22) и отчетному году (2024)."""
    dict_res = client.get("/api/dictionaries")
    industries = dict_res.json()["industries"]
    consulting = next(ind for ind in industries if ind["code"] == "70.22")

    response = client.get(
        "/api/analytics/top-revenue",
        params={"industry_id": consulting["id"], "year": 2024},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
    assert all(item["okved_code"] == "70.22" and item["report_year"] == 2024 for item in data)


def test_06_top_revenue_demo_case(client: TestClient):
    """Проверка главного демо-кейса /api/analytics/top-revenue: не пустой и отсортирован по убыванию выручки."""
    response = client.get("/api/analytics/top-revenue", params={"year": 2024})
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 20
    assert data[0]["revenue"] >= data[1]["revenue"]
    for i in range(len(data) - 1):
        assert data[i]["revenue"] >= data[i + 1]["revenue"]
    # Проверяем наличие пересчета в рубли через units.to_rub
    assert data[0]["revenue_rub"] == data[0]["revenue"] * data[0]["to_rub"]


def test_07_export_csv(client: TestClient):
    """Проверка /api/export?format=csv: код 200, text/csv, наличие UTF-8 BOM и заголовков."""
    response = client.get("/api/export", params={"format": "csv", "year": 2024})
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    raw_bytes = response.content
    assert raw_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM для MS Excel
    text = raw_bytes.decode("utf-8-sig")
    assert "ИНН;КПП;ОГРН;Наименование организации" in text
    assert "2918010863" in text


def test_08_export_json_and_xlsx(client: TestClient):
    """Проверка выгрузки выборки в форматах JSON и XLSX."""
    res_json = client.get("/api/export", params={"format": "json", "year": 2024})
    assert res_json.status_code == 200
    json_list = res_json.json()
    assert isinstance(json_list, list)
    assert len(json_list) >= 20

    res_xlsx = client.get("/api/export", params={"format": "xlsx", "year": 2024})
    assert res_xlsx.status_code == 200
    assert "spreadsheetml" in res_xlsx.headers.get("content-type", "")
    assert res_xlsx.content.startswith(b"PK")  # ZIP/XLSX сигнатура


def test_09_import_valid_xml_and_json(client: TestClient):
    """Загрузка валидных XML 5.08 и JSON через POST /api/import, проверка БД, идемпотентности и import_files."""
    sample_dir = Path("sample_data")
    xml_path = sample_dir / "sample_508_full.xml"
    json_path = sample_dir / "sample_report.json"

    with xml_path.open("rb") as f:
        res_xml = client.post(
            "/api/import",
            files={"file": ("sample_508_full.xml", f, "application/xml")},
        )
    assert res_xml.status_code == 200
    xml_payload = res_xml.json()
    assert xml_payload["inn"] == "7701998877"
    assert xml_payload["status"] in ("success", "duplicate")

    # Повторная загрузка того же файла должна сработать идемпотентно по SHA-256
    with xml_path.open("rb") as f:
        res_dup = client.post(
            "/api/import",
            files={"file": ("sample_508_full.xml", f, "application/xml")},
        )
    assert res_dup.status_code == 200
    assert res_dup.json()["duplicate_prevented"] is True

    # Загрузка JSON-отчета
    with json_path.open("rb") as f:
        res_json = client.post(
            "/api/import",
            files={"file": ("sample_report.json", f, "application/json")},
        )
    assert res_json.status_code == 200
    assert res_json.json()["inn"] == "1655889900"

    # Проверяем наличие в журнале import_files
    logs_res = client.get("/api/import-logs")
    assert logs_res.status_code == 200
    file_names = [item["file_name"] for item in logs_res.json()]
    assert "sample_508_full.xml" in file_names
    assert "sample_report.json" in file_names


def test_10_import_invalid_file_format(client: TestClient):
    """Загрузка неподдерживаемого файла .txt возвращает HTTP 400."""
    response = client.post(
        "/api/import",
        files={"file": ("unsupported_notes.txt", b"invalid plain text content", "text/plain")},
    )
    assert response.status_code == 400
    assert "Неподдерживаемый формат" in response.json()["detail"]
