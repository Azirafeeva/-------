from datetime import datetime, date
from sqlalchemy import (
    Column,
    Integer,
    SmallInteger,
    BigInteger,
    String,
    Text,
    Boolean,
    Numeric,
    Date,
    DateTime,
    ForeignKey,
    CheckConstraint,
    Index,
    JSON,
    func,
)
from sqlalchemy.orm import relationship
from app.database import Base

# Use Integer on SQLite for autoincrement PK compatibility and BigInteger on PostgreSQL
PKBigInt = Integer().with_variant(BigInteger, "postgresql")
PKSmallInt = Integer().with_variant(SmallInteger, "postgresql")


# ============================================================================
# СЛОЙ 1. СПРАВОЧНИКИ
# ============================================================================

class Region(Base):
    __tablename__ = "regions"

    id = Column(PKSmallInt, primary_key=True, autoincrement=True)
    code = Column(String(2), unique=True, nullable=False, index=True)
    name = Column(Text, nullable=False)
    federal_district = Column(Text, nullable=True)

    organizations = relationship("Organization", back_populates="region")


class Industry(Base):
    __tablename__ = "industries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(8), unique=True, nullable=False, index=True)
    name = Column(Text, nullable=False)
    parent_id = Column(Integer, ForeignKey("industries.id", ondelete="SET NULL"), nullable=True)
    section = Column(String(1), nullable=True)

    parent = relationship("Industry", remote_side=[id], backref="children")
    organizations = relationship("Organization", back_populates="industry")


class Unit(Base):
    __tablename__ = "units"

    code = Column(String(3), primary_key=True)
    name = Column(Text, nullable=False)
    to_rub = Column(Numeric(18, 4), nullable=False, default=1)

    reports = relationship("Report", back_populates="unit")


class ReportForm(Base):
    __tablename__ = "report_forms"

    id = Column(PKSmallInt, primary_key=True, autoincrement=True)
    code = Column(String(4), unique=True, nullable=False, index=True)
    name = Column(Text, nullable=False)

    indicators = relationship("Indicator", back_populates="form")
    reports = relationship("Report", back_populates="form")
    slices = relationship("ValueSlice", back_populates="form")


class Indicator(Base):
    __tablename__ = "indicators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), unique=True, nullable=False, index=True)
    name = Column(Text, nullable=False)
    form_id = Column(PKSmallInt, ForeignKey("report_forms.id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(Integer, ForeignKey("indicators.id", ondelete="SET NULL"), nullable=True)
    is_total = Column(Boolean, nullable=False, default=False)
    is_aggregated = Column(Boolean, nullable=False, default=False)

    form = relationship("ReportForm", back_populates="indicators")
    parent = relationship("Indicator", remote_side=[id], backref="children")
    sources = relationship("IndicatorSource", back_populates="indicator", cascade="all, delete-orphan")
    values = relationship("ReportValue", back_populates="indicator")


class IndicatorSource(Base):
    __tablename__ = "indicator_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    indicator_id = Column(Integer, ForeignKey("indicators.id", ondelete="CASCADE"), nullable=False)
    format_version = Column(String(8), nullable=False)
    form_code = Column(String(4), ForeignKey("report_forms.code", ondelete="CASCADE"), nullable=False)
    xml_path = Column(Text, nullable=False)

    indicator = relationship("Indicator", back_populates="sources")

    __table_args__ = (
        Index("idx_indicator_sources_ver_path", "format_version", "xml_path"),
    )


class ValueSlice(Base):
    __tablename__ = "value_slices"

    id = Column(PKSmallInt, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(Text, nullable=False)
    form_id = Column(PKSmallInt, ForeignKey("report_forms.id", ondelete="SET NULL"), nullable=True)

    form = relationship("ReportForm", back_populates="slices")
    values = relationship("ReportValue", back_populates="slice")


# ============================================================================
# СЛОЙ 2. ЯДРО УЧЕТА
# ============================================================================

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(PKBigInt, primary_key=True, autoincrement=True)
    inn = Column(String(12), unique=True, nullable=False, index=True)
    ogrn = Column(String(15), nullable=True, index=True)
    okpo = Column(String(10), nullable=True)
    name_full = Column(Text, nullable=False)
    name_short = Column(Text, nullable=True)
    okved_id = Column(Integer, ForeignKey("industries.id", ondelete="SET NULL"), nullable=True, index=True)
    okopf = Column(String(5), nullable=True)
    okfs = Column(String(3), nullable=True)
    region_id = Column(PKSmallInt, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True, index=True)
    region_source = Column(String(32), default="address")
    size_class = Column(Text, nullable=False, default="Малое")
    size_source = Column(String(32), nullable=True)
    address_raw = Column(Text, nullable=True)
    address_norm = Column(JSON, nullable=True)
    signer_type = Column(Text, default="director")
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    region = relationship("Region", back_populates="organizations")
    industry = relationship("Industry", back_populates="organizations")
    packages = relationship("ReportPackage", back_populates="organization", cascade="all, delete-orphan")


class ReportPackage(Base):
    __tablename__ = "report_packages"

    id = Column(PKBigInt, primary_key=True, autoincrement=True)
    org_id = Column(PKBigInt, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    kpp = Column(String(9), nullable=False, default="")
    report_year = Column(SmallInteger, nullable=False, index=True)
    period_code = Column(String(4), nullable=False, default="34")
    period_end = Column(Date, nullable=False)
    correction_no = Column(SmallInteger, nullable=False, default=0)
    knd = Column(String(7), nullable=False, default="0710099")
    format_version = Column(String(8), nullable=False, default="5.08")
    generator = Column(Text, nullable=True)
    doc_date = Column(Date, nullable=True)
    audit_flag = Column(Boolean, nullable=True, default=False)
    signer = Column(JSON, nullable=True)
    is_current = Column(Boolean, nullable=False, default=True, index=True)
    superseded_by = Column(PKBigInt, ForeignKey("report_packages.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    organization = relationship("Organization", back_populates="packages")
    reports = relationship("Report", back_populates="package", cascade="all, delete-orphan")
    import_files = relationship("ImportFile", back_populates="package")


class Report(Base):
    __tablename__ = "reports"

    id = Column(PKBigInt, primary_key=True, autoincrement=True)
    package_id = Column(PKBigInt, ForeignKey("report_packages.id", ondelete="CASCADE"), nullable=False, index=True)
    form_id = Column(PKSmallInt, ForeignKey("report_forms.id", ondelete="RESTRICT"), nullable=False, index=True)
    section_code = Column(String(32), nullable=False, default="")
    okud = Column(String(7), nullable=True)
    unit_code = Column(String(3), ForeignKey("units.code", ondelete="RESTRICT"), nullable=False, default="384")
    source_path = Column(Text, nullable=True)

    package = relationship("ReportPackage", back_populates="reports")
    form = relationship("ReportForm", back_populates="reports")
    unit = relationship("Unit", back_populates="reports")
    values = relationship("ReportValue", back_populates="report", cascade="all, delete-orphan")


class ReportValue(Base):
    __tablename__ = "report_values"

    id = Column(PKBigInt, primary_key=True, autoincrement=True)
    report_id = Column(PKBigInt, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    indicator_id = Column(Integer, ForeignKey("indicators.id", ondelete="RESTRICT"), nullable=False, index=True)
    slice_id = Column(PKSmallInt, ForeignKey("value_slices.id", ondelete="RESTRICT"), nullable=False, default=1)
    as_of = Column(Date, nullable=False)
    report_year = Column(SmallInteger, nullable=False, index=True)
    value = Column(Numeric(20, 2), nullable=True)
    status = Column(Text, nullable=False, default="reported")
    source_column = Column(String(16), nullable=False, default="СумОтч")
    is_comparative = Column(Boolean, nullable=False, default=False, index=True)

    report = relationship("Report", back_populates="values")
    indicator = relationship("Indicator", back_populates="values")
    slice = relationship("ValueSlice", back_populates="values")

    __table_args__ = (
        CheckConstraint(
            "(status = 'reported' AND value IS NOT NULL) OR "
            "(status = 'zero' AND value = 0) OR "
            "(status IN ('not_disclosed', 'not_applicable') AND value IS NULL)",
            name="chk_report_values_status",
        ),
    )


# ============================================================================
# СЛОЙ 3. СЛУЖЕБНЫЙ КОНТУР
# ============================================================================

class ImportFile(Base):
    __tablename__ = "import_files"

    id = Column(PKBigInt, primary_key=True, autoincrement=True)
    sha256 = Column(String(64), unique=True, nullable=False, index=True)
    file_name = Column(Text, nullable=False)
    source_encoding = Column(String(16), nullable=True)
    format_version = Column(String(8), nullable=True)
    raw_xml = Column(Text, nullable=True)
    storage_uri = Column(Text, nullable=True)
    package_id = Column(PKBigInt, ForeignKey("report_packages.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    error = Column(JSON, nullable=True)
    received_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    package = relationship("ReportPackage", back_populates="import_files")
