import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
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


def seed_database(db: Session) -> None:
    """Заполняет БД эталонными справочниками, 25 организациями за 2023-2025 гг. и генерирует sample_data."""
    Base.metadata.create_all(bind=engine)

    if db.query(Organization).count() > 0:
        generate_sample_files()
        return

    # 1. РЕГИОНЫ РФ
    regions_data = [
        ("29", "Архангельская область", "СЗФО"),
        ("16", "Республика Татарстан", "ПФО"),
        ("71", "Тульская область", "ЦФО"),
        ("52", "Нижегородская область", "ПФО"),
        ("77", "г. Москва", "ЦФО"),
        ("78", "г. Санкт-Петербург", "СЗФО"),
        ("55", "Омская область", "СФО"),
        ("61", "Ростовская область", "ЮФО"),
        ("74", "Челябинская область", "УФО"),
    ]
    regions_map = {}
    for code, name, fd in regions_data:
        reg = Region(code=code, name=name, federal_district=fd)
        db.add(reg)
        db.flush()
        regions_map[code] = reg

    # 2. ОТРАСЛИ ОКВЭД2 (С ИЕРАРХИЕЙ parent_id)
    parent_industries_data = [
        ("64", "Деятельность по предоставлению финансовых услуг, кроме услуг по страхованию и пенсионному обеспечению", "K"),
        ("70", "Деятельность головных офисов; консультирование по вопросам управления", "M"),
        ("47", "Торговля розничная, кроме торговли автотранспортными средствами и мотоциклами", "G"),
        ("62", "Разработка компьютерного программного обеспечения, консультационные услуги в данной области", "J"),
        ("41", "Строительство зданий", "F"),
    ]
    parent_ind_map = {}
    for code, name, sec in parent_industries_data:
        ind = Industry(code=code, name=name, parent_id=None, section=sec)
        db.add(ind)
        db.flush()
        parent_ind_map[code] = ind

    child_industries_data = [
        ("64.92.1", "Деятельность по предоставлению потребительских кредитов", "64", "K"),
        ("70.22", "Консультирование по вопросам коммерческой деятельности и управления", "70", "M"),
        ("64.99", "Деятельность прочая финансовая, не включенная в другие группировки", "64", "K"),
        ("47.77.2", "Торговля розничная ювелирными изделиями в специализированных магазинах", "47", "G"),
        ("62.01", "Разработка компьютерного программного обеспечения", "62", "J"),
        ("41.20", "Строительство жилых и нежилых зданий", "41", "F"),
    ]
    industries_map = dict(parent_ind_map)
    for code, name, p_code, sec in child_industries_data:
        ind = Industry(
            code=code,
            name=name,
            parent_id=parent_ind_map[p_code].id,
            section=sec,
        )
        db.add(ind)
        db.flush()
        industries_map[code] = ind

    # 3. ЕДИНИЦЫ ИЗМЕРЕНИЯ (ОКЕИ)
    units_data = [
        ("383", "Рубль", Decimal("1")),
        ("384", "Тысяча рублей", Decimal("1000")),
        ("385", "Миллион рублей", Decimal("1000000")),
    ]
    for code, name, to_rub in units_data:
        db.add(Unit(code=code, name=name, to_rub=to_rub))
    db.flush()

    # 4. ФОРМЫ ОТЧЕТНОСТИ (F1..F5)
    forms_data = [
        ("F1", "Бухгалтерский баланс"),
        ("F2", "Отчет о финансовых результатах"),
        ("F3", "Отчет об изменениях капитала"),
        ("F4", "Отчет о движении денежных средств"),
        ("F5", "Пояснения к бухгалтерскому балансу"),
    ]
    forms_map = {}
    for code, name in forms_data:
        frm = ReportForm(code=code, name=name)
        db.add(frm)
        db.flush()
        forms_map[code] = frm

    # 5. КАНОНИЧЕСКИЙ СЛОВАРЬ ПОКАЗАТЕЛЕЙ (Приказ Минфина №66н)
    indicators_data = [
        # Форма 1: Бухгалтерский баланс
        ("1600", "Баланс (Актив)", "F1", None, True, False),
        ("1100", "Внеоборотные активы — Итого по разделу I", "F1", "1600", True, False),
        ("1110", "Нематериальные активы", "F1", "1100", False, False),
        ("1150", "Основные средства", "F1", "1100", False, False),
        ("1200", "Оборотные активы — Итого по разделу II", "F1", "1600", True, False),
        ("1210", "Запасы", "F1", "1200", False, False),
        ("1230", "Дебиторская задолженность", "F1", "1200", False, False),
        ("1250", "Денежные средства и денежные эквиваленты", "F1", "1200", False, False),
        ("1700", "Баланс (Пассив)", "F1", None, True, False),
        ("1300", "Капитал и резервы — Итого по разделу III", "F1", "1700", True, False),
        ("1310", "Уставный капитал (складочный капитал, уставный фонд, вклады товарищей)", "F1", "1300", False, False),
        ("1400", "Долгосрочные обязательства — Итого по разделу IV", "F1", "1700", True, False),
        ("1500", "Краткосрочные обязательства — Итого по разделу V", "F1", "1700", True, False),
        ("1520", "Кредиторская задолженность", "F1", "1500", False, False),
        ("UPR_KREDIT", "Кредиторская задолженность (упрощенная форма)", "F1", "1700", False, True),
        # Форма 2: Отчет о финансовых результатах
        ("2100", "Валовая прибыль (убыток)", "F2", None, True, False),
        ("2110", "Выручка", "F2", "2100", False, False),
        ("2120", "Себестоимость продаж", "F2", "2100", False, False),
        ("2200", "Прибыль (убыток) от продаж", "F2", None, True, False),
        ("2300", "Прибыль (убыток) до налогообложения", "F2", None, True, False),
        ("2400", "Чистая прибыль (убыток)", "F2", None, True, False),
        # Форма 3: Отчет об изменениях капитала
        ("3100", "Величина капитала на 31 декабря позапрошлого года (начало года)", "F3", None, False, False),
        ("3200", "Величина капитала на 31 декабря предыдущего года (конец периода)", "F3", None, False, False),
        ("3300", "Чистые активы", "F3", None, False, False),
        # Форма 4: Отчет о движении денежных средств
        ("4100", "Сальдо денежных потоков от текущих операций", "F4", None, True, False),
        ("4110", "Поступления от текущих операций — всего", "F4", "4100", False, False),
        ("4120", "Платежи по текущим операциям — всего", "F4", "4100", False, False),
        # Форма 5: Пояснения к бухгалтерскому балансу
        ("5100", "Наличие и движение нематериальных активов", "F5", None, False, False),
        ("5200", "Наличие и движение основных средств", "F5", None, False, False),
        ("5400", "Наличие и движение запасов", "F5", None, False, False),
    ]
    indicators_map = {}
    for code, name, form_code, parent_code, is_total, is_agg in indicators_data:
        parent_id = indicators_map[parent_code].id if parent_code and parent_code in indicators_map else None
        ind = Indicator(
            code=code,
            name=name,
            form_id=forms_map[form_code].id,
            parent_id=parent_id,
            is_total=is_total,
            is_aggregated=is_agg,
        )
        db.add(ind)
        db.flush()
        indicators_map[code] = ind

    # 6. МОСТ XPath МЕЖДУ ВЕРСИЯМИ XML ФНС (5.03 / 5.08) И СЛОВАРЕМ ПОКАЗАТЕЛЕЙ
    xpath_mappings = [
        # Обязательные маппинги из ТЗ:
        ("/Документ/Баланс/Актив/ОбА/Запасы", "5.08", "F1", "1210"),
        ("/Документ/Баланс/Актив/Запасы", "5.03", "F1", "1210"),
        ("/Документ/Баланс/Актив/ОбА/ДебЗад", "5.08", "F1", "1230"),
        ("/Документ/Баланс/Актив/ДебЗад", "5.03", "F1", "1230"),
        ("/Документ/Баланс/Актив/ОбА/ДенежнСр", "5.08", "F1", "1250"),
        ("/Документ/Баланс/Актив/ДенежнСр", "5.03", "F1", "1250"),
        ("/Документ/Баланс/Актив", "5.08", "F1", "1600"),
        ("/Документ/Баланс/Актив", "5.03", "F1", "1600"),
        ("/Документ/Баланс/Пассив/КапРез/УставКапитал", "5.08", "F1", "1310"),
        ("/Документ/Баланс/Пассив/КапРез", "5.03", "F1", "1300"),
        ("/Документ/Баланс/Пассив/КраткосрочОб/КредитЗадолж", "5.08", "F1", "1520"),
        ("/Документ/Баланс/Пассив/КредитЗадолж", "5.03", "F1", "UPR_KREDIT"),
        ("/Документ/Баланс/Пассив", "5.08", "F1", "1700"),
        ("/Документ/Баланс/Пассив", "5.03", "F1", "1700"),
        ("/Документ/ФинРез/Выруч", "5.08", "F2", "2110"),
        ("/Документ/ФинРез/Выруч", "5.03", "F2", "2110"),
        ("/Документ/ФинРез/СебестПрод", "5.08", "F2", "2120"),
        ("/Документ/ФинРез/РасхОбщДеят", "5.03", "F2", "2120"),
        ("/Документ/ФинРез/ВаловПриб", "5.08", "F2", "2100"),
        ("/Документ/ФинРез/ПрибУбДоНал", "5.08", "F2", "2300"),
        ("/Документ/ФинРез/ЧистПрибУб", "5.08", "F2", "2400"),
        ("/Документ/ФинРез/ЧистПрибУб", "5.03", "F2", "2400"),
        # Дополнительные маппинги для полной поддержки баланса и реальных выгрузок ФНС (БОО2024):
        ("/Документ/Баланс/Актив/ВнеОбА", "5.08", "F1", "1100"),
        ("/Документ/Баланс/Актив/ВнеобА", "5.08", "F1", "1100"),
        ("/Документ/Баланс/Актив/ВнеОбА/НематАкт", "5.08", "F1", "1110"),
        ("/Документ/Баланс/Актив/ВнеобА/НМА", "5.08", "F1", "1110"),
        ("/Документ/Баланс/Актив/НеМатФинАкт", "5.03", "F1", "1110"),
        ("/Документ/Баланс/Актив/ВнеОбА/ОснСр", "5.08", "F1", "1150"),
        ("/Документ/Баланс/Актив/ВнеобА/ОснСр", "5.08", "F1", "1150"),
        ("/Документ/Баланс/Актив/МатВнеАкт", "5.03", "F1", "1150"),
        ("/Документ/Баланс/Актив/МатВнеобАкт", "5.03", "F1", "1150"),
        ("/Документ/Баланс/Актив/ОбА", "5.08", "F1", "1200"),
        ("/Документ/Баланс/Актив/ФинВлож", "5.03", "F1", "1230"),
        ("/Документ/Баланс/Пассив/КапРез", "5.08", "F1", "1300"),
        ("/Документ/Баланс/Пассив/ДолгосрОбяз", "5.08", "F1", "1400"),
        ("/Документ/Баланс/Пассив/ДолгосрочОб", "5.08", "F1", "1400"),
        ("/Документ/Баланс/Пассив/ДлгЗаемСредств", "5.03", "F1", "1400"),
        ("/Документ/Баланс/Пассив/КраткосрОбяз", "5.08", "F1", "1500"),
        ("/Документ/Баланс/Пассив/КраткосрочОб", "5.08", "F1", "1500"),
        ("/Документ/Баланс/Пассив/КртЗаемСредств", "5.03", "F1", "1500"),
        ("/Документ/Баланс/Пассив/КраткосрОбяз/КредитЗадолж", "5.08", "F1", "1520"),
        ("/Документ/ФинРез/РасхОбДеят", "5.03", "F2", "2120"),
        ("/Документ/ФинРез/ВаловаяПрибыль", "5.08", "F2", "2100"),
        ("/Документ/ФинРез/ПрибПрод", "5.08", "F2", "2200"),
        ("/Документ/ФинРез/ПрибУбПрод", "5.08", "F2", "2200"),
        # Маппинги Формы 3 (Отчет об изменениях капитала) и Формы 4 (ОДДС):
        ("/Документ/ОтчетИзмКап/ДвиженКап/Кап31ДекПред", "5.08", "F3", "3100"),
        ("/Документ/ОтчетИзмКап/ДвиженКап/ОтчетГод/Кап31дек", "5.08", "F3", "3200"),
        ("/Документ/ОтчетИзмКап/ЧистАктив", "5.08", "F3", "3300"),
        ("/Документ/ДвижениеДен/ТекОпер/Поступ", "5.08", "F4", "4110"),
        ("/Документ/ДвижениеДен/ТекОпер/Платеж", "5.08", "F4", "4120"),
        ("/Документ/ДвижениеДен/ТекОпер/СальдоТек", "5.08", "F4", "4100"),
        ("/Документ/ДвижениеДен/СальдоОтч", "5.03", "F4", "4100"),
    ]
    for xml_path, fmt_ver, form_code, ind_code in xpath_mappings:
        db.add(
            IndicatorSource(
                indicator_id=indicators_map[ind_code].id,
                format_version=fmt_ver,
                form_code=form_code,
                xml_path=xml_path,
            )
        )
    db.flush()

    # 7. СРЕЗЫ МАТРИЧНЫХ ФОРМ (value_slices)
    slices_data = [
        ("total", "Итого (скалярная форма)", None),
        ("ustav", "Уставный капитал", "F3"),
        ("dobav", "Добавочный капитал", "F3"),
        ("rezerv", "Резервный капитал", "F3"),
        ("operational", "Операционная деятельность", "F4"),
        ("investing", "Инвестиционная деятельность", "F4"),
        ("financing", "Финансовая деятельность", "F4"),
        ("original", "Первоначальная стоимость", "F5"),
        ("depreciation", "Накопленная амортизация", "F5"),
        ("residual", "Остаточная стоимость", "F5"),
    ]
    slices_map = {}
    for code, name, f_code in slices_data:
        sl = ValueSlice(
            code=code,
            name=name,
            form_id=forms_map[f_code].id if f_code else None,
        )
        db.add(sl)
        db.flush()
        slices_map[code] = sl

    # 8. ОРГАНИЗАЦИИ (25 КОМПАНИЙ: 8 ЭТАЛОННЫХ ИЗ ФНС + 17 ДЕМО-ОРГАНИЗАЦИЙ)
    companies = [
        {
            "inn": "2918010863",
            "kpp": "291801001",
            "ogrn": "1152918000412",
            "okpo": "26775017",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЛЕГКОЗАЙМ ПЛЮС"',
            "name_short": 'ООО "Легкозайм плюс"',
            "region": "29",
            "okved": "64.92.1",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Микропредприятие",
            "address": "165300, Архангельская обл., г. Котлас, пр-кт Мира, д. 18",
            "signer": "Бовконюк Светлана Павловна",
            "signer_type": "director",
            "generator": "Налогоплательщик ЮЛ 4.86.1",
            "knd": "0710096",
            "format_version": "5.03",
            "base_rev": 48500,
            "margin": 0.18,
        },
        {
            "inn": "1648049858",
            "kpp": "164801001",
            "ogrn": "1181690071230",
            "okpo": "39494113",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КОНСАЛТГРУПП"',
            "name_short": 'ООО "КОНСАЛТГРУПП"',
            "region": "16",
            "okved": "70.22",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "422540, Республика Татарстан, г. Зеленодольск, ул. Ленина, д. 41А",
            "signer": "Лапин Юрий Николаевич",
            "signer_type": "director",
            "generator": "СБИС 3.23.7155",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 195400,
            "margin": 0.22,
        },
        {
            "inn": "7100010051",
            "kpp": "710001001",
            "ogrn": "1217100008914",
            "okpo": "55973801",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ФИНИНВЕСТ-ТУЛА"',
            "name_short": 'ООО "ФинИнвест-Тула"',
            "region": "71",
            "okved": "64.99",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "300041, Тульская обл., г. Тула, Красноармейский пр-кт, д. 7",
            "signer": "Любимова Ирина Юрьевна (по доверенности)",
            "signer_type": "representative",
            "generator": "1С:Предприятие 8.3 (3.0.147)",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 640200,
            "margin": 0.16,
        },
        {
            "inn": "5249056650",
            "kpp": "524901001",
            "ogrn": "1025201756810",
            "okpo": "55081971",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЮВЕЛИРТОРГ-НН"',
            "name_short": 'ООО "ЮвелирТорг-НН"',
            "region": "52",
            "okved": "47.77.2",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "606000, Нижегородская обл., г. Дзержинск, пр-кт Циолковского, д. 34",
            "signer": "Смирнов Алексей Викторович",
            "signer_type": "director",
            "generator": "Контур.Экстерн 24.1",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 312800,
            "margin": 0.14,
        },
        {
            "inn": "7804606556",
            "kpp": "780401001",
            "ogrn": "1177847301256",
            "okpo": "19485712",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СЕВЕРО-ЗАПАДНЫЕ ЦИФРОВЫЕ СИСТЕМЫ"',
            "name_short": 'ООО "СЗЦС"',
            "region": "78",
            "okved": "62.01",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Крупное",
            "address": "195009, г. Санкт-Петербург, Кондратьевский пр-кт, д. 15, корп. 3",
            "signer": "Воронов Дмитрий Сергеевич",
            "signer_type": "director",
            "generator": "СБИС 3.24.1102",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 2480000,
            "margin": 0.24,
        },
        {
            "inn": "5507048753",
            "kpp": "550701001",
            "ogrn": "1045504011982",
            "okpo": "71239045",
            "name_full": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "ОМСКСТРОЙИНДУСТРИЯ"',
            "name_short": 'АО "ОмскСтройИндустрия"',
            "region": "55",
            "okved": "41.20",
            "okopf": "12267",
            "okfs": "16",
            "size_class": "Крупное",
            "address": "644074, Омская обл., г. Омск, ул. 70 лет Октября, д. 25",
            "signer": "Коваленко Игорь Петрович",
            "signer_type": "director",
            "generator": "1С:Предприятие 8.3 ERP",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 3150000,
            "margin": 0.11,
        },
        {
            "inn": "6132013021",
            "kpp": "613201001",
            "ogrn": "1126132000541",
            "okpo": "90812341",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЮГ-ФИНАНС КОНСАЛТИНГ"',
            "name_short": 'ООО "Юг-Финанс Консалтинг"',
            "region": "61",
            "okved": "70.22",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "344002, Ростовская обл., г. Ростов-на-Дону, ул. Большая Садовая, д. 88",
            "signer": "Ткаченко Елена Андреевна",
            "signer_type": "director",
            "generator": "Налогоплательщик ЮЛ 4.87",
            "knd": "0710096",
            "format_version": "5.03",
            "base_rev": 124500,
            "margin": 0.19,
        },
        {
            "inn": "7456004565",
            "kpp": "745601001",
            "ogrn": "1117456009812",
            "okpo": "68912034",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "УРАЛПРОМСТРОЙ"',
            "name_short": 'ООО "УралПромСтрой"',
            "region": "74",
            "okved": "41.20",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "455000, Челябинская обл., г. Магнитогорск, пр-кт Ленина, д. 52",
            "signer": "Захаров Андрей Михайлович",
            "signer_type": "director",
            "generator": "Контур.Экстерн 24.2",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 980400,
            "margin": 0.09,
        },
        {
            "inn": "7707812340",
            "kpp": "770701001",
            "ogrn": "1147746123450",
            "okpo": "29384756",
            "name_full": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "МОСКОВСКИЙ АНАЛИТИЧЕСКИЙ ЦЕНТР ИНФОРМАЦИОННЫХ ТЕХНОЛОГИЙ"',
            "name_short": 'АО "МАЦИТ"',
            "region": "77",
            "okved": "62.01",
            "okopf": "12267",
            "okfs": "16",
            "size_class": "Крупное",
            "address": "107078, г. Москва, ул. Мясницкая, д. 39, стр. 1",
            "signer": "Громов Виктор Александрович",
            "signer_type": "director",
            "generator": "1С:Холдинг 8.3",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 5420000,
            "margin": 0.27,
        },
        {
            "inn": "7715908712",
            "kpp": "771501001",
            "ogrn": "1167746509811",
            "okpo": "45109283",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СТОЛИЧНЫЙ КАПИТАЛ ИНВЕСТ"',
            "name_short": 'ООО "Столичный Капитал Инвест"',
            "region": "77",
            "okved": "64.99",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Крупное",
            "address": "123112, г. Москва, Пресненская наб., д. 12",
            "signer": "Белова Наталия Владимировна",
            "signer_type": "director",
            "generator": "СБИС 3.24.1102",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 4190000,
            "margin": 0.21,
        },
        {
            "inn": "1655309182",
            "kpp": "165501001",
            "ogrn": "1151690021984",
            "okpo": "33190842",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КАЗАНЬ СОФТ ЛАБ"',
            "name_short": 'ООО "Казань Софт Лаб"',
            "region": "16",
            "okved": "62.01",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "420107, Республика Татарстан, г. Казань, ул. Петербургская, д. 52",
            "signer": "Сафин Тимур Рустамович",
            "signer_type": "director",
            "generator": "Контур.Экстерн 24.1",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 875000,
            "margin": 0.25,
        },
        {
            "inn": "5260412980",
            "kpp": "526001001",
            "ogrn": "1175275031209",
            "okpo": "44901287",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ВОЛГА-ДЕВЕЛОПМЕНТ ГРУПП"',
            "name_short": 'ООО "Волга-Девелопмент Групп"',
            "region": "52",
            "okved": "41.20",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "603000, Нижегородская обл., г. Нижний Новгород, ул. Горького, д. 117",
            "signer": "Морозов Павел Геннадьевич",
            "signer_type": "director",
            "generator": "1С:Предприятие 8.3",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 1430000,
            "margin": 0.12,
        },
        {
            "inn": "7813290415",
            "kpp": "781301001",
            "ogrn": "1187847091234",
            "okpo": "51209384",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "БАЛТИЙСКИЙ ЮВЕЛИРНЫЙ ДОМ"',
            "name_short": 'ООО "Балтийский Ювелирный Дом"',
            "region": "78",
            "okved": "47.77.2",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "197101, г. Санкт-Петербург, Каменноостровский пр-кт, д. 26",
            "signer": "Орлова Ксения Игоревна",
            "signer_type": "director",
            "generator": "СБИС 3.23.7155",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 760000,
            "margin": 0.15,
        },
        {
            "inn": "2901289451",
            "kpp": "290101001",
            "ogrn": "1162901054321",
            "okpo": "28910456",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ПОМОРЬЕ СТРОЙМОНТАЖ"',
            "name_short": 'ООО "Поморье СтройМонтаж"',
            "region": "29",
            "okved": "41.20",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "163000, Архангельская обл., г. Архангельск, наб. Северной Двины, д. 93",
            "signer": "Попов Николай Федорович",
            "signer_type": "director",
            "generator": "1С:Бухгалтерия 8.3",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 284000,
            "margin": 0.08,
        },
        {
            "inn": "7106078123",
            "kpp": "710601001",
            "ogrn": "1197154006789",
            "okpo": "60192837",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТУЛА-АЙТИ РЕШЕНИЯ"',
            "name_short": 'ООО "Тула-АйТи Решения"',
            "region": "71",
            "okved": "62.01",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "300012, Тульская обл., г. Тула, пр-кт Ленина, д. 85",
            "signer": "Данилов Артем Олегович",
            "signer_type": "director",
            "generator": "Контур.Экстерн 24.1",
            "knd": "0710096",
            "format_version": "5.03",
            "base_rev": 168000,
            "margin": 0.28,
        },
        {
            "inn": "5503190284",
            "kpp": "550301001",
            "ogrn": "1175543019283",
            "okpo": "77120934",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СИБИРСКИЙ КРЕДИТНЫЙ СОЮЗ"',
            "name_short": 'ООО "Сибирский Кредитный Союз"',
            "region": "55",
            "okved": "64.92.1",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Микропредприятие",
            "address": "644043, Омская обл., г. Омск, ул. Красный Путь, д. 32",
            "signer": "Мельников Сергей Анатольевич",
            "signer_type": "director",
            "generator": "Налогоплательщик ЮЛ 4.86.1",
            "knd": "0710096",
            "format_version": "5.03",
            "base_rev": 62300,
            "margin": 0.17,
        },
        {
            "inn": "6164290184",
            "kpp": "616401001",
            "ogrn": "1156196041290",
            "okpo": "88192034",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ДОНСКОЙ ЮВЕЛИРНЫЙ ЦЕНТР"',
            "name_short": 'ООО "Донской Ювелирный Центр"',
            "region": "61",
            "okved": "47.77.2",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "344082, Ростовская обл., г. Ростов-на-Дону, пр-кт Буденновский, д. 49",
            "signer": "Кравцова Марина Юрьевна",
            "signer_type": "director",
            "generator": "СБИС 3.23.7155",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 238000,
            "margin": 0.13,
        },
        {
            "inn": "7451390128",
            "kpp": "745101001",
            "ogrn": "1167456081234",
            "okpo": "66109283",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЧЕЛЯБИНСК БИЗНЕС КОНСАЛТ"',
            "name_short": 'ООО "Челябинск Бизнес Консалт"',
            "region": "74",
            "okved": "70.22",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Микропредприятие",
            "address": "454091, Челябинская обл., г. Челябинск, пр-кт Ленина, д. 21В",
            "signer": "Кузнецов Роман Денисович",
            "signer_type": "director",
            "generator": "Налогоплательщик ЮЛ 4.86.1",
            "knd": "0710096",
            "format_version": "5.03",
            "base_rev": 54900,
            "margin": 0.21,
        },
        {
            "inn": "7728450912",
            "kpp": "772801001",
            "ogrn": "1187746890123",
            "okpo": "31092847",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЦЕНТРАЛЬНОЕ АГЕНТСТВО УПРАВЛЕНЧЕСКОГО КОНСАЛТИНГА"',
            "name_short": 'ООО "ЦАУК"',
            "region": "77",
            "okved": "70.22",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "117342, г. Москва, ул. Профсоюзная, д. 65, корп. 1",
            "signer": "Лебедев Максим Станиславович",
            "signer_type": "director",
            "generator": "1С:Предприятие 8.3",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 1120000,
            "margin": 0.20,
        },
        {
            "inn": "7841089124",
            "kpp": "784101001",
            "ogrn": "1197847120934",
            "okpo": "48901234",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "НЕВСКИЙ ФИНАНСОВЫЙ АЛЬЯНС"',
            "name_short": 'ООО "Невский Финансовый Альянс"',
            "region": "78",
            "okved": "64.99",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Среднее",
            "address": "191186, г. Санкт-Петербург, Невский пр-кт, д. 38",
            "signer": "Соколов Кирилл Вадимович",
            "signer_type": "director",
            "generator": "СБИС 3.24.1102",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 910000,
            "margin": 0.16,
        },
        {
            "inn": "1659128390",
            "kpp": "165901001",
            "ogrn": "1201600045123",
            "okpo": "52901823",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТАТСТРОЙПРОЕКТ"',
            "name_short": 'ООО "ТатСтройПроект"',
            "region": "16",
            "okved": "41.20",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Крупное",
            "address": "420059, Республика Татарстан, г. Казань, Оренбургский тракт, д. 20",
            "signer": "Галимов Руслан Ильдарович",
            "signer_type": "director",
            "generator": "1С:ERP Управление предприятием 2",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 2740000,
            "margin": 0.10,
        },
        {
            "inn": "5257109283",
            "kpp": "525701001",
            "ogrn": "1185275061289",
            "okpo": "41928374",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "НИЖЕГОРОДСКИЕ ПРОГРАММНЫЕ ПЛАТФОРМЫ"',
            "name_short": 'ООО "НПП"',
            "region": "52",
            "okved": "62.01",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "603155, Нижегородская обл., г. Нижний Новгород, ул. Большая Печерская, д. 40",
            "signer": "Федоров Евгений Борисович",
            "signer_type": "director",
            "generator": "Контур.Экстерн 24.2",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 345000,
            "margin": 0.26,
        },
        {
            "inn": "6168091245",
            "kpp": "616801001",
            "ogrn": "1176196029384",
            "okpo": "83910294",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "АЗОВ-ТЕХНОСТРОЙ"',
            "name_short": 'ООО "Азов-ТехноСтрой"',
            "region": "61",
            "okved": "41.20",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "344091, Ростовская обл., г. Ростов-на-Дону, пр-кт Стачки, д. 190",
            "signer": "Шевченко Олег Григорьевич",
            "signer_type": "director",
            "generator": "1С:Бухгалтерия 8.3",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 410000,
            "margin": -0.04,  # одна убыточная для реалистичной доли прибыльных компаний
        },
        {
            "inn": "7453091827",
            "kpp": "745301001",
            "ogrn": "1207400019283",
            "okpo": "64910283",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЮЖНО-УРАЛЬСКИЙ ЦЕНТР ЦИФРОВИЗАЦИИ"',
            "name_short": 'ООО "ЮУЦЦ"',
            "region": "74",
            "okved": "62.01",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Малое",
            "address": "454080, Челябинская обл., г. Челябинск, пр-кт Сони Кривой, д. 75",
            "signer": "Абрамов Денис Вячеславович",
            "signer_type": "director",
            "generator": "СБИС 3.24.1102",
            "knd": "0710099",
            "format_version": "5.08",
            "base_rev": 292000,
            "margin": 0.23,
        },
        {
            "inn": "2902091845",
            "kpp": "290201001",
            "ogrn": "1182901009182",
            "okpo": "25901827",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СЕВЕРНЫЙ ФИНАНСОВЫЙ БРОКЕР"',
            "name_short": 'ООО "Северный Финансовый Брокер"',
            "region": "29",
            "okved": "64.99",
            "okopf": "12300",
            "okfs": "16",
            "size_class": "Микропредприятие",
            "address": "164500, Архангельская обл., г. Северодвинск, пр-кт Ломоносова, д. 71",
            "signer": "Беляев Константин Петрович",
            "signer_type": "director",
            "generator": "Налогоплательщик ЮЛ 4.86.1",
            "knd": "0710096",
            "format_version": "5.03",
            "base_rev": 76400,
            "margin": 0.15,
        },
    ]

    years = [2023, 2024, 2025]
    growth_factors = {2023: 0.87, 2024: 1.00, 2025: 1.14}

    for idx, c in enumerate(companies):
        reg = regions_map[c["region"]]
        ind = industries_map[c["okved"]]
        org = Organization(
            inn=c["inn"],
            ogrn=c["ogrn"],
            okpo=c["okpo"],
            name_full=c["name_full"],
            name_short=c["name_short"],
            okved_id=ind.id,
            okopf=c["okopf"],
            okfs=c["okfs"],
            region_id=reg.id,
            region_source="address",
            size_class=c["size_class"],
            size_source="rmsp_registry",
            address_raw=c["address"],
            address_norm={
                "region_code": c["region"],
                "region_name": reg.name,
                "federal_district": reg.federal_district,
                "raw": c["address"],
            },
            signer_type=c["signer_type"],
        )
        db.add(org)
        db.flush()

        for yr in years:
            gf = growth_factors[yr]
            rev = round(c["base_rev"] * gf)
            cost = round(rev * (1.0 - max(c["margin"], 0.05) - 0.08))
            gross = rev - cost
            op_profit = round(gross * 0.82)
            pre_tax = round(rev * (c["margin"] * 1.25))
            net_profit = round(rev * c["margin"])

            # Баланс (Форма 1)
            nma = round(rev * 0.04)
            os_val = round(rev * 0.28)
            vneob = nma + os_val

            zapasy = round(rev * 0.14)
            deb_zad = round(rev * 0.19)
            money = round(rev * 0.11)
            oborot = zapasy + deb_zad + money

            active_total = vneob + oborot
            ustav = 10000 if c["size_class"] in ("Крупное", "Среднее") else 100
            capital = round(active_total * 0.52)
            long_obl = round(active_total * 0.18)
            short_obl = active_total - capital - long_obl
            kredit_zad = round(short_obl * 0.78)
            passive_total = active_total

            pkg = ReportPackage(
                org_id=org.id,
                kpp=c["kpp"],
                report_year=yr,
                period_code="34",
                period_end=date(yr, 12, 31),
                correction_no=1 if (idx < 3 and yr == 2024) else 0,
                knd=c["knd"],
                format_version=c["format_version"],
                generator=c["generator"],
                doc_date=date(yr + 1, 3, 25),
                audit_flag=(c["size_class"] == "Крупное"),
                signer={
                    "full_name": c["signer"],
                    "signer_type": c["signer_type"],
                },
                is_current=True,
            )
            db.add(pkg)
            db.flush()

            # Создаем разделы F1..F5
            rep_f1 = Report(
                package_id=pkg.id,
                form_id=forms_map["F1"].id,
                section_code="F1",
                okud="0710001",
                unit_code="384",
                source_path="/Файл/Документ/Баланс",
            )
            rep_f2 = Report(
                package_id=pkg.id,
                form_id=forms_map["F2"].id,
                section_code="F2",
                okud="0710002",
                unit_code="384",
                source_path="/Файл/Документ/ФинРез",
            )
            rep_f3 = Report(
                package_id=pkg.id,
                form_id=forms_map["F3"].id,
                section_code="F3",
                okud="0710004",
                unit_code="384",
                source_path="/Файл/Документ/ИзмКап",
            )
            rep_f4 = Report(
                package_id=pkg.id,
                form_id=forms_map["F4"].id,
                section_code="F4",
                okud="0710005",
                unit_code="384",
                source_path="/Файл/Документ/ДвижДен",
            )
            rep_f5 = Report(
                package_id=pkg.id,
                form_id=forms_map["F5"].id,
                section_code="F5",
                okud="0710006",
                unit_code="384",
                source_path="/Файл/Документ/ПояснББ",
            )
            db.add_all([rep_f1, rep_f2, rep_f3, rep_f4, rep_f5])
            db.flush()

            as_of_dt = date(yr, 12, 31)
            sl_total = slices_map["total"].id

            f1_vals = [
                ("1100", vneob, "total"),
                ("1110", nma, "total"),
                ("1150", os_val, "total"),
                ("1200", oborot, "total"),
                ("1210", zapasy, "total"),
                ("1230", deb_zad, "total"),
                ("1250", money, "total"),
                ("1600", active_total, "total"),
                ("1300", capital, "total"),
                ("1310", ustav, "total"),
                ("1400", long_obl, "total"),
                ("1500", short_obl, "total"),
                ("1520", kredit_zad, "total"),
                ("1700", passive_total, "total"),
            ]
            if c["format_version"] == "5.03":
                f1_vals.append(("UPR_KREDIT", kredit_zad, "total"))

            for code, val, sl_code in f1_vals:
                db.add(
                    ReportValue(
                        report_id=rep_f1.id,
                        indicator_id=indicators_map[code].id,
                        slice_id=slices_map[sl_code].id,
                        as_of=as_of_dt,
                        report_year=yr,
                        value=Decimal(str(val)),
                        status="reported" if val != 0 else "zero",
                        source_column="СумОтч",
                        is_comparative=False,
                    )
                )

            f2_vals = [
                ("2110", rev),
                ("2120", cost),
                ("2100", gross),
                ("2200", op_profit),
                ("2300", pre_tax),
                ("2400", net_profit),
            ]
            for code, val in f2_vals:
                db.add(
                    ReportValue(
                        report_id=rep_f2.id,
                        indicator_id=indicators_map[code].id,
                        slice_id=sl_total,
                        as_of=as_of_dt,
                        report_year=yr,
                        value=Decimal(str(val)),
                        status="reported" if val != 0 else "zero",
                        source_column="СумОтч",
                        is_comparative=False,
                    )
                )

            f3_vals = [
                ("3100", round(capital * 0.88), "total"),
                ("3200", capital, "total"),
                ("3200", ustav, "ustav"),
                ("3200", round(capital * 0.15), "dobav"),
                ("3200", round(capital * 0.05), "rezerv"),
                ("3300", round(capital * 1.03), "total"),
            ]
            for code, val, sl_code in f3_vals:
                db.add(
                    ReportValue(
                        report_id=rep_f3.id,
                        indicator_id=indicators_map[code].id,
                        slice_id=slices_map[sl_code].id,
                        as_of=as_of_dt,
                        report_year=yr,
                        value=Decimal(str(val)),
                        status="reported" if val != 0 else "zero",
                        source_column="СумОтч",
                        is_comparative=False,
                    )
                )

            cf_in = round(rev * 1.08)
            cf_out = round(cost * 1.05)
            cf_net = cf_in - cf_out
            f4_vals = [
                ("4110", cf_in, "operational"),
                ("4120", cf_out, "operational"),
                ("4100", cf_net, "operational"),
            ]
            for code, val, sl_code in f4_vals:
                db.add(
                    ReportValue(
                        report_id=rep_f4.id,
                        indicator_id=indicators_map[code].id,
                        slice_id=slices_map[sl_code].id,
                        as_of=as_of_dt,
                        report_year=yr,
                        value=Decimal(str(val)),
                        status="reported" if val != 0 else "zero",
                        source_column="СумОтч",
                        is_comparative=False,
                    )
                )

            f5_vals = [
                ("5100", round(nma * 1.3), "original"),
                ("5100", round(nma * 0.3), "depreciation"),
                ("5100", nma, "residual"),
                ("5200", round(os_val * 1.4), "original"),
                ("5200", round(os_val * 0.4), "depreciation"),
                ("5200", os_val, "residual"),
                ("5400", zapasy, "residual"),
            ]
            for code, val, sl_code in f5_vals:
                db.add(
                    ReportValue(
                        report_id=rep_f5.id,
                        indicator_id=indicators_map[code].id,
                        slice_id=slices_map[sl_code].id,
                        as_of=as_of_dt,
                        report_year=yr,
                        value=Decimal(str(val)),
                        status="reported" if val != 0 else "zero",
                        source_column="СумОтч",
                        is_comparative=False,
                    )
                )

            # Журнал импорта для ключевых организаций за 2024 год
            if yr == 2024 and idx < 8:
                raw_seed_tag = f"<Файл ИдФайл='NO_BUHOTCH_{c['inn']}_{yr}' ВерсФорм='{c['format_version']}'/>"
                sha = hashlib.sha256(raw_seed_tag.encode("utf-8")).hexdigest()
                db.add(
                    ImportFile(
                        sha256=sha,
                        file_name=f"NO_BUHOTCH_{c['inn'][:4]}_{c['inn']}{c['kpp']}_{yr}1231.xml",
                        source_encoding="windows-1251" if c["format_version"] == "5.03" else "UTF-8",
                        format_version=c["format_version"],
                        raw_xml=raw_seed_tag,
                        storage_uri=f"seed://archive/{yr}/{c['inn']}.xml",
                        package_id=pkg.id,
                        status="success",
                    )
                )

    db.commit()
    generate_sample_files()


def generate_sample_files(base_dir: str = "sample_data") -> None:
    """
    Генерирует 4 эталонных файла в папке sample_data/ для демонстрации импорта:
    - sample_508_full.xml (КНД 0710099, ВерсФорм 5.08)
    - sample_503_simpl.xml (КНД 0710096, ВерсФорм 5.03)
    - sample_report.xlsx (Плоский шаблон Excel)
    - sample_report.json (Плоский шаблон JSON)
    """
    out_dir = Path(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. sample_508_full.xml (КНД 0710099, ВерсФорм 5.08)
    xml_508 = """<?xml version="1.0" encoding="UTF-8"?>
<Файл ИдФайл="NO_BUHOTCH_7701_7701998877770101001_20250325_001" ВерсПрог="1С:Предприятие 8.3 (3.0.150)" ВерсФорм="5.08">
  <Документ КНД="0710099" ДатаДок="25.03.2025" ОтчетГод="2024" Период="34" НомКорр="0" ОКЕИ="384" ПризнАудит="1">
    <СвНП ОКПО="40192831" ОКОПФ="12300" ОКФС="16" ОКВЭД2="62.01">
      <НПЮЛ НаимОрг="ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ &quot;ЦИФРОВАЯ ПЛАТФОРМА ГОССТАТА&quot;" НаимОргСокр="ООО &quot;ЦП Госстата&quot;" ИННЮЛ="7701998877" КПП="770101001" ОГРН="1227700112233" КатСуб="Среднее">
        <АдрМН Индекс="105064" КодРегион="77" Город="Москва" Улица="ул. Земляной Вал" Дом="д. 44"/>
      </НПЮЛ>
    </СвНП>
    <Подписант ПрПодп="1">
      <ФИО Фамилия="Косов" Имя="Григорий" Отчество="Алексеевич"/>
    </Подписант>
    <Баланс ОКУД="0710001">
      <Актив СумОтч="1580000" СумПрдщ="1320000" СумПрдшв="1140000">
        <ВнеобА СумОтч="520000" СумПрдщ="450000" СумПрдшв="390000">
          <НМА СумОтч="140000" СумПрдщ="110000" СумПрдшв="90000"/>
          <ОснСр СумОтч="380000" СумПрдщ="340000" СумПрдшв="300000"/>
        </ВнеобА>
        <ОбА СумОтч="1060000" СумПрдщ="870000" СумПрдшв="750000">
          <Запасы СумОтч="210000" СумПрдщ="180000" СумПрдшв="150000"/>
          <ДебЗад СумОтч="490000" СумПрдщ="410000" СумПрдшв="360000"/>
          <ДенежнСр СумОтч="360000" СумПрдщ="280000" СумПрдшв="240000"/>
        </ОбА>
      </Актив>
      <Пассив СумОтч="1580000" СумПрдщ="1320000" СумПрдшв="1140000">
        <КапРез СумОтч="920000" СумПрдщ="740000" СумПрдшв="610000">
          <УставКапитал СумОтч="10000" СумПрдщ="10000" СумПрдшв="10000"/>
        </КапРез>
        <ДолгосрочОб СумОтч="260000" СумПрдщ="240000" СумПрдшв="220000"/>
        <КраткосрочОб СумОтч="400000" СумПрдщ="340000" СумПрдшв="310000">
          <КредитЗадолж СумОтч="315000" СумПрдщ="275000" СумПрдшв="250000"/>
        </КраткосрочОб>
      </Пассив>
    </Баланс>
    <ФинРез ОКУД="0710002">
      <Выруч СумОтч="2150000" СумПред="1780000"/>
      <СебестПрод СумОтч="1340000" СумПред="1150000"/>
      <ВаловПриб СумОтч="810000" СумПред="630000"/>
      <ПрибУбПрод СумОтч="640000" СумПред="490000"/>
      <ПрибУбДоНал СумОтч="610000" СумПред="460000"/>
      <ЧистПрибУб СумОтч="488000" СумПред="368000"/>
    </ФинРез>
  </Документ>
</Файл>
"""
    (out_dir / "sample_508_full.xml").write_text(xml_508, encoding="utf-8")

    # 2. sample_503_simpl.xml (КНД 0710096, ВерсФорм 5.03)
    xml_503 = """<?xml version="1.0" encoding="UTF-8"?>
<Файл ИдФайл="NO_BOUPR_2901_2901554433290101001_20250320_002" ВерсПрог="Налогоплательщик ЮЛ 4.88" ВерсФорм="5.03">
  <Документ КНД="0710096" ДатаДок="20.03.2025" ОтчетГод="2024" Период="34" НомКорр="0" ОКЕИ="384">
    <СвНП ОКПО="29887766" ОКОПФ="12300" ОКФС="16" ОКВЭД2="64.92.1">
      <НПЮЛ НаимОрг="ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ &quot;СЕВЕРКРЕДИТ-ЭКСПРЕСС&quot;" НаимОргСокр="ООО &quot;СеверКредит-Экспресс&quot;" ИННЮЛ="2901554433" КПП="290101001" ОГРН="1192901005544" КатСуб="Малое">
        <АдрМН Индекс="163002" КодРегион="29" Город="Архангельск" Улица="пр-кт Троицкий" Дом="д. 102"/>
      </НПЮЛ>
    </СвНП>
    <Подписант ПрПодп="1">
      <ФИО Фамилия="Волков" Имя="Владимир" Отчество="Сергеевич"/>
    </Подписант>
    <Баланс ОКУД="0710001">
      <Актив СумОтч="145000" СумПрдщ="118000" СумПрдшв="96000">
        <МатВнеобАкт СумОтч="32000" СумПрдщ="28000" СумПрдшв="24000"/>
        <Запасы СумОтч="18000" СумПрдщ="15000" СумПрдшв="12000"/>
        <ДебЗад СумОтч="64000" СумПрдщ="51000" СумПрдшв="42000"/>
        <ДенежнСр СумОтч="31000" СумПрдщ="24000" СумПрдшв="18000"/>
      </Актив>
      <Пассив СумОтч="145000" СумПрдщ="118000" СумПрдшв="96000">
        <КапРез СумОтч="89000" СумПрдщ="71000" СумПрдшв="58000"/>
        <КредитЗадолж СумОтч="56000" СумПрдщ="47000" СумПрдшв="38000"/>
      </Пассив>
    </Баланс>
    <ФинРез ОКУД="0710002">
      <Выруч СумОтч="218000" СумПред="174000"/>
      <РасхОбщДеят СумОтч="162000" СумПред="133000"/>
      <ЧистПрибУб СумОтч="44800" СумПред="32800"/>
    </ФинРез>
  </Документ>
</Файл>
"""
    (out_dir / "sample_503_simpl.xml").write_text(xml_503, encoding="utf-8")

    # 3. sample_report.json
    sample_json = {
        "format_version": "5.08",
        "knd": "0710099",
        "report_year": 2024,
        "correction_no": 0,
        "unit_code": "384",
        "generator": "ГИР БО JSON Коннектор 1.0",
        "organization": {
            "inn": "1655889900",
            "kpp": "165501001",
            "ogrn": "1211600099887",
            "okpo": "44556677",
            "name_full": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ИННОПОЛИС ДАТА АНАЛИТИКА"',
            "name_short": 'ООО "Иннополис Дата Аналитика"',
            "region_code": "16",
            "okved": "62.01",
            "size_class": "Среднее",
            "address": "420500, Республика Татарстан, г. Иннополис, ул. Университетская, д. 7",
        },
        "signer": {
            "role": "Руководитель",
            "full_name": "Бельданова Анна Игоревна",
        },
        "values": {
            "1100": 240000,
            "1200": 510000,
            "1210": 85000,
            "1230": 245000,
            "1250": 180000,
            "1600": 750000,
            "1300": 480000,
            "1310": 1000,
            "1400": 90000,
            "1500": 180000,
            "1520": 140000,
            "1700": 750000,
            "2110": 1290000,
            "2120": 840000,
            "2100": 450000,
            "2200": 370000,
            "2300": 350000,
            "2400": 280000,
        },
    }
    (out_dir / "sample_report.json").write_text(
        json.dumps(sample_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 4. sample_report.xlsx
    rows = [
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "2110", "value": 945000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "2120", "value": 680000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "2100", "value": 265000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "2400", "value": 182000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "1100", "value": 210000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "1200", "value": 390000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "1600", "value": 600000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "1300", "value": 350000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "1520", "value": 160000},
        {"inn": "5260991122", "name_full": 'ООО "ВОЛЖСКИЙ ИНЖИНИРИНГОВЫЙ ЦЕНТР"', "report_year": 2024, "region_code": "52", "okved": "70.22", "size_class": "Среднее", "indicator_code": "1700", "value": 600000},
    ]
    df = pd.DataFrame(rows)
    df.to_excel(out_dir / "sample_report.xlsx", index=False)


if __name__ == "__main__":
    session = SessionLocal()
    try:
        seed_database(session)
        print("База данных успешно проинициализирована и заполнена демонстрационными данными (25 организаций, 2023-2025 гг.).")
        print("Примеры файлов для импорта созданы в папке sample_data/.")
    finally:
        session.close()
