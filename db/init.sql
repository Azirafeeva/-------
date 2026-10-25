-- ============================================================================
-- ФГБУ ГМЦ Росстата: Эталонная схема БД бухгалтерской отчетности (PostgreSQL 15)
-- Архитектура: 3 слоя, 12 таблиц (Приказ Минфина №66н, форматы ФНС 5.03 и 5.08)
-- ============================================================================

-- ============================================================================
-- СЛОЙ 1. СПРАВОЧНИКИ И МЕТАДАННЫЕ
-- ============================================================================

-- 1. Справочник регионов РФ
CREATE TABLE IF NOT EXISTS regions (
    id SMALLSERIAL PRIMARY KEY,
    code VARCHAR(2) UNIQUE NOT NULL,
    name TEXT NOT NULL,
    federal_district TEXT
);

CREATE INDEX IF NOT EXISTS idx_regions_code ON regions(code);

-- 2. Иерархический справочник видов экономической деятельности (ОКВЭД2)
CREATE TABLE IF NOT EXISTS industries (
    id SERIAL PRIMARY KEY,
    code VARCHAR(8) UNIQUE NOT NULL,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES industries(id) ON DELETE SET NULL,
    section CHAR(1)
);

CREATE INDEX IF NOT EXISTS idx_industries_code ON industries(code);
CREATE INDEX IF NOT EXISTS idx_industries_parent ON industries(parent_id);

-- 3. Справочник единиц измерения (ОКЕИ)
CREATE TABLE IF NOT EXISTS units (
    code VARCHAR(3) PRIMARY KEY,
    name TEXT NOT NULL,
    to_rub NUMERIC(18, 4) NOT NULL DEFAULT 1
);

-- 4. Справочник форм бухгалтерской отчетности
CREATE TABLE IF NOT EXISTS report_forms (
    id SMALLSERIAL PRIMARY KEY,
    code VARCHAR(4) UNIQUE NOT NULL,
    name TEXT NOT NULL
);

-- 5. Канонический словарь показателей по Приказу Минфина №66н
CREATE TABLE IF NOT EXISTS indicators (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    name TEXT NOT NULL,
    form_id SMALLINT NOT NULL REFERENCES report_forms(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES indicators(id) ON DELETE SET NULL,
    is_total BOOLEAN NOT NULL DEFAULT FALSE,
    is_aggregated BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_indicators_form ON indicators(form_id);
CREATE INDEX IF NOT EXISTS idx_indicators_code ON indicators(code);

-- 6. Мост XPath между версиями XML ФНС (5.03 / 5.08) и каноническим словарем
CREATE TABLE IF NOT EXISTS indicator_sources (
    id SERIAL PRIMARY KEY,
    indicator_id INTEGER NOT NULL REFERENCES indicators(id) ON DELETE CASCADE,
    format_version VARCHAR(8) NOT NULL CHECK (format_version IN ('5.03', '5.08')),
    form_code VARCHAR(4) NOT NULL REFERENCES report_forms(code) ON DELETE CASCADE,
    xml_path TEXT NOT NULL,
    UNIQUE (format_version, form_code, xml_path)
);

CREATE INDEX IF NOT EXISTS idx_indicator_sources_lookup ON indicator_sources(format_version, xml_path);

-- 7. Измерения матричных форм (Формы 3, 4, 5 и скалярный итог)
CREATE TABLE IF NOT EXISTS value_slices (
    id SMALLSERIAL PRIMARY KEY,
    code VARCHAR(32) UNIQUE NOT NULL,
    name TEXT NOT NULL,
    form_id SMALLINT REFERENCES report_forms(id) ON DELETE SET NULL
);

-- ============================================================================
-- СЛОЙ 2. ЯДРО УЧЕТА ОТЧЕТНОСТИ
-- ============================================================================

-- 8. Карточка отчитывающейся организации
CREATE TABLE IF NOT EXISTS organizations (
    id BIGSERIAL PRIMARY KEY,
    inn VARCHAR(12) UNIQUE NOT NULL,
    ogrn VARCHAR(15),
    okpo VARCHAR(10),
    name_full TEXT NOT NULL,
    name_short TEXT,
    okved_id INTEGER REFERENCES industries(id) ON DELETE SET NULL,
    okopf VARCHAR(5),
    okfs VARCHAR(3),
    region_id SMALLINT REFERENCES regions(id) ON DELETE SET NULL,
    region_source VARCHAR(32) DEFAULT 'address',
    size_class TEXT NOT NULL DEFAULT 'Малое',
    size_source VARCHAR(32),
    address_raw TEXT,
    address_norm JSONB,
    signer_type TEXT DEFAULT 'director',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_organizations_inn ON organizations(inn);
CREATE INDEX IF NOT EXISTS idx_organizations_ogrn ON organizations(ogrn);
CREATE INDEX IF NOT EXISTS idx_organizations_region ON organizations(region_id);
CREATE INDEX IF NOT EXISTS idx_organizations_okved ON organizations(okved_id);
CREATE INDEX IF NOT EXISTS idx_organizations_size ON organizations(size_class);

-- 9. Комплект бухгалтерской отчетности (документ ФНС КНД 0710099 / 0710096)
CREATE TABLE IF NOT EXISTS report_packages (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    kpp VARCHAR(9) NOT NULL DEFAULT '',
    report_year SMALLINT NOT NULL CHECK (report_year BETWEEN 1990 AND 2100),
    period_code VARCHAR(4) NOT NULL DEFAULT '34',
    period_end DATE NOT NULL,
    correction_no SMALLINT NOT NULL DEFAULT 0 CHECK (correction_no >= 0),
    knd VARCHAR(7) NOT NULL DEFAULT '0710099',
    format_version VARCHAR(8) NOT NULL DEFAULT '5.08',
    generator TEXT,
    doc_date DATE,
    audit_flag BOOLEAN,
    signer JSONB,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_by BIGINT REFERENCES report_packages(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_packages_org_year_current ON report_packages(org_id, report_year, is_current);
CREATE INDEX IF NOT EXISTS idx_packages_year_current ON report_packages(report_year, is_current);

-- 10. Раздел / форма внутри комплекта отчетности
CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    package_id BIGINT NOT NULL REFERENCES report_packages(id) ON DELETE CASCADE,
    form_id SMALLINT NOT NULL REFERENCES report_forms(id) ON DELETE RESTRICT,
    section_code VARCHAR(32) NOT NULL DEFAULT '',
    okud VARCHAR(7),
    unit_code VARCHAR(3) NOT NULL DEFAULT '384' REFERENCES units(code) ON DELETE RESTRICT,
    source_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_reports_package_form ON reports(package_id, form_id);

-- 11. Фактические значения показателей бухгалтерской отчетности
CREATE TABLE IF NOT EXISTS report_values (
    id BIGSERIAL PRIMARY KEY,
    report_id BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    indicator_id INTEGER NOT NULL REFERENCES indicators(id) ON DELETE RESTRICT,
    slice_id SMALLINT NOT NULL DEFAULT 1 REFERENCES value_slices(id) ON DELETE RESTRICT,
    as_of DATE NOT NULL,
    report_year SMALLINT NOT NULL,
    value NUMERIC(20, 2),
    status TEXT NOT NULL DEFAULT 'reported',
    source_column VARCHAR(16) NOT NULL DEFAULT 'СумОтч',
    is_comparative BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT chk_report_values_status CHECK (
        (status = 'reported' AND value IS NOT NULL) OR
        (status = 'zero' AND value = 0) OR
        (status IN ('not_disclosed', 'not_applicable') AND value IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_report_values_report ON report_values(report_id);
CREATE INDEX IF NOT EXISTS idx_report_values_indicator_year ON report_values(indicator_id, report_year, is_comparative);

-- ============================================================================
-- СЛОЙ 3. СЛУЖЕБНЫЙ КОНТУР И ЖУРНАЛ ИМПОРТА
-- ============================================================================

-- 12. Журнал импорта файлов для обеспечения идемпотентности (SHA-256)
CREATE TABLE IF NOT EXISTS import_files (
    id BIGSERIAL PRIMARY KEY,
    sha256 VARCHAR(64) UNIQUE NOT NULL,
    file_name TEXT NOT NULL,
    source_encoding VARCHAR(16),
    format_version VARCHAR(8),
    raw_xml TEXT,
    storage_uri TEXT,
    package_id BIGINT REFERENCES report_packages(id) ON DELETE SET NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    error JSONB,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_import_files_sha256 ON import_files(sha256);
CREATE INDEX IF NOT EXISTS idx_import_files_status ON import_files(status);
