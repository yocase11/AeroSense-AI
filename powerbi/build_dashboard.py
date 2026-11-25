from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_NAME = "AeroSensePowerBI"
TABLE = "vw_pm25_hourly"
VISUAL_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/visualContainer/2.7.0/schema.json"
)


def _id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aerosense-powerbi:{key}"))


def _column(
    name: str,
    kind: str,
    format_string: str | None = None,
    sort_by: str | None = None,
) -> str:
    lines = [
        f"column {name}",
        f"\tdataType: {kind}",
        "\tsummarizeBy: none",
        f"\tsourceColumn: {name}",
        f"\tlineageTag: {_id(f'column:{name}')}",
    ]
    if format_string:
        lines.insert(2, f"\tformatString: {format_string}")
    if sort_by:
        lines.append(f"\tsortByColumn: {sort_by}")
    return "\n".join(lines)


def _field(kind: str, name: str) -> dict[str, Any]:
    return {
        kind: {
            "Expression": {"SourceRef": {"Entity": TABLE}},
            "Property": name,
        }
    }


def _projection(kind: str, name: str, *, active: bool = False) -> dict[str, Any]:
    field = _field(kind, name)
    projection: dict[str, Any] = {
        "field": field,
        "queryRef": f"{TABLE}.{name}",
        "nativeQueryRef": name,
    }
    if active:
        projection["active"] = True
    return projection


def _visual(
    identity: str,
    visual_type: str,
    x: int,
    y: int,
    width: int,
    height: int,
    query_state: dict[str, list[dict[str, Any]]],
    tab_order: int,
) -> dict[str, Any]:
    return {
        "$schema": VISUAL_SCHEMA,
        "name": identity,
        "position": {
            "x": x,
            "y": y,
            "z": tab_order,
            "height": height,
            "width": width,
            "tabOrder": tab_order,
        },
        "visual": {
            "visualType": visual_type,
            "query": {
                "queryState": {
                    role: {"projections": projections}
                    for role, projections in query_state.items()
                }
            },
            "drillFilterOtherVisuals": True,
        },
    }


def _field_visual(
    identity: str,
    visual_type: str,
    field_kind: str,
    field_name: str,
    x: int,
    y: int,
    width: int,
    height: int,
    tab_order: int,
) -> dict[str, Any]:
    return _visual(
        identity,
        visual_type,
        x,
        y,
        width,
        height,
        {"Values": [_projection(field_kind, field_name, active=True)]},
        tab_order,
    )


def _measure_visual(
    identity: str,
    name: str,
    x: int,
    y: int,
    width: int,
    height: int,
    tab_order: int,
) -> dict[str, Any]:
    return _visual(
        identity,
        "card",
        x,
        y,
        width,
        height,
        {"Values": [_projection("Measure", name)]},
        tab_order,
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _page(
    report_dir: Path,
    folder_name: str,
    display_name: str,
    visuals: list[tuple[str, dict[str, Any]]],
) -> str:
    page_id = _id(f"page:{folder_name}").replace("-", "")[:20]
    page_dir = report_dir / "definition" / "pages" / folder_name
    _write_json(
        page_dir / "page.json",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "report/definition/page/2.0.0/schema.json"
            ),
            "name": page_id,
            "displayName": display_name,
            "displayOption": "FitToPage",
            "height": 720,
            "width": 1280,
        },
    )
    for folder, visual in visuals:
        _write_json(page_dir / "visuals" / folder / "visual.json", visual)
    return page_id


def _table_tmdl() -> str:
    columns = [
        ("city_id", "string", None),
        ("city_name", "string", None),
        ("hour_utc", "dateTime", "General Date"),
        ("hour_ist", "dateTime", "General Date"),
        ("local_hour", "int64", "0"),
        ("local_weekday_num", "int64", "0"),
        ("local_weekday", "string", None),
        ("pm25_mean_ug_m3", "double", "0.0"),
        ("pm25_measurement_count", "int64", "0"),
        ("pm25_station_count", "int64", "0"),
        ("temperature_c", "double", "0.0"),
        ("wind_speed_kmh", "double", "0.0"),
        ("humidity_pct", "double", "0.0"),
        ("precipitation_mm", "double", "0.00"),
    ]
    lines = [f"table {TABLE}", f"\tlineageTag: {_id(f'table:{TABLE}')}", ""]
    measures = [
        (
            "Mean PM2.5 (µg/m³)",
            "AVERAGE('vw_pm25_hourly'[pm25_mean_ug_m3])",
            "0.0",
        ),
        ("Observed City-Hours", "COUNTROWS('vw_pm25_hourly')", "#,0"),
        (
            "Mean Wind Speed (km/h)",
            "AVERAGE('vw_pm25_hourly'[wind_speed_kmh])",
            "0.0",
        ),
        (
            "Mean Stations per City-Hour",
            "AVERAGE('vw_pm25_hourly'[pm25_station_count])",
            "0.0",
        ),
    ]
    for name, expression, format_string in measures:
        lines.extend(
            [
                f"\tmeasure '{name}' = {expression}",
                f"\t\tformatString: {format_string}",
                f"\t\tlineageTag: {_id(f'measure:{name}')}",
                "",
            ]
        )
    for name, kind, format_string in columns:
        lines.extend(
            [
                _column(
                    name,
                    kind,
                    format_string,
                    "local_weekday_num" if name == "local_weekday" else None,
                ),
                "",
            ]
        )
    lines.extend(
        [
            f"\tpartition {TABLE} = m",
            "\t\tmode: import",
            "\t\tsource =",
            "\t\t\tlet",
            '\t\t\t\tSource = PostgreSQL.Database("localhost:5432", "aerosense", [Query = "SELECT * FROM public.vw_pm25_hourly"]),',
            '\t\t\t\tTyped = Table.TransformColumnTypes(Source, {{',
            '\t\t\t\t\t{"hour_utc", type datetime}, {"hour_ist", type datetime},',
            '\t\t\t\t\t{"local_hour", Int64.Type}, {"local_weekday_num", Int64.Type},',
            '\t\t\t\t\t{"pm25_mean_ug_m3", type number}, {"pm25_measurement_count", Int64.Type},',
            '\t\t\t\t\t{"pm25_station_count", Int64.Type}, {"temperature_c", type number},',
            '\t\t\t\t\t{"wind_speed_kmh", type number}, {"humidity_pct", type number},',
            '\t\t\t\t\t{"precipitation_mm", type number}',
            "\t\t\t\t}})",
            "\t\t\tin",
            "\t\t\t\tTyped",
            "",
        ]
    )
    return "\n".join(lines)


def build_dashboard(output_dir: Path) -> Path:
    project_dir = output_dir / PROJECT_NAME
    report_dir = project_dir / f"{PROJECT_NAME}.Report"
    model_dir = project_dir / f"{PROJECT_NAME}.SemanticModel"
    definition_dir = report_dir / "definition"
    tables_dir = model_dir / "definition" / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        project_dir / f"{PROJECT_NAME}.pbip",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/pbip/"
                "pbipProperties/1.0.0/schema.json"
            ),
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{PROJECT_NAME}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        },
    )
    _write_json(
        report_dir / "definition.pbir",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "report/definitionProperties/2.0.0/schema.json"
            ),
            "version": "4.0",
            "datasetReference": {
                "byPath": {"path": f"../{PROJECT_NAME}.SemanticModel"}
            },
        },
    )
    _write_json(
        model_dir / "definition.pbism",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "semanticModel/definitionProperties/1.0.0/schema.json"
            ),
            "version": "4.0",
            "settings": {},
        },
    )
    (model_dir / "definition" / "database.tmdl").write_text(
        f"database {PROJECT_NAME}\n", encoding="utf-8"
    )
    (model_dir / "definition" / "model.tmdl").write_text(
        "model Model\n"
        "\tculture: en-US\n"
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n"
        "\tdataAccessOptions\n"
        "\t\tlegacyRedirects\n"
        "\t\treturnErrorValuesAsNull\n"
        "\n"
        f"ref table {TABLE}\n",
        encoding="utf-8",
    )
    (tables_dir / f"{TABLE}.tmdl").write_text(
        _table_tmdl(), encoding="utf-8"
    )
    _write_json(
        definition_dir / "version.json",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "report/definition/versionMetadata/1.0.0/schema.json"
            ),
            "version": "2.0.0",
        },
    )
    _write_json(
        definition_dir / "report.json",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "report/definition/report/3.0.0/schema.json"
            ),
            "themeCollection": {
                "customTheme": {
                    "name": "AeroSenseTheme.json",
                    "reportVersionAtImport": {
                        "visual": "2.7.0",
                        "report": "3.0.0",
                        "page": "2.0.0",
                    },
                    "type": "RegisteredResources",
                }
            },
            "filterConfig": {"filters": []},
            "settings": {
                "useStylableVisualContainerHeader": True,
                "defaultDrillFilterOtherVisuals": True,
                "useEnhancedTooltips": True,
            },
            "resourcePackages": [
                {
                    "name": "RegisteredResources",
                    "type": "RegisteredResources",
                    "items": [
                        {
                            "name": "AeroSenseTheme.json",
                            "path": "AeroSenseTheme.json",
                            "type": "CustomTheme",
                        }
                    ],
                }
            ],
        },
    )
    _write_json(
        report_dir
        / "StaticResources"
        / "RegisteredResources"
        / "AeroSenseTheme.json",
        {
            "name": "AeroSense",
            "dataColors": [
                "#0E7490",
                "#F97316",
                "#4F46E5",
                "#16A34A",
                "#D946EF",
                "#EAB308",
                "#DC2626",
            ],
            "background": "#F5F7FA",
            "foreground": "#15324B",
            "tableAccent": "#0E7490",
        },
    )

    def slicers() -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                "City_Slicer",
                _field_visual(
                    "city_slicer",
                    "slicer",
                    "Column",
                    "city_name",
                    24,
                    20,
                    260,
                    82,
                    0,
                ),
            ),
            (
                "Date_Slicer",
                _field_visual(
                    "date_slicer",
                    "slicer",
                    "Column",
                    "hour_ist",
                    300,
                    20,
                    430,
                    82,
                    1,
                ),
            ),
        ]

    overview = slicers() + [
        (
            "Mean_PM25_Card",
            _measure_visual(
                "mean_pm25_card", "Mean PM2.5 (µg/m³)", 24, 116, 240, 92, 2
            ),
        ),
        (
            "City_Hours_Card",
            _measure_visual(
                "city_hours_card", "Observed City-Hours", 276, 116, 220, 92, 3
            ),
        ),
        (
            "Mean_Wind_Card",
            _measure_visual(
                "mean_wind_card", "Mean Wind Speed (km/h)", 508, 116, 222, 92, 4
            ),
        ),
        (
            "PM25_Trend",
            _visual(
                "pm25_trend",
                "lineChart",
                24,
                224,
                770,
                472,
                {
                    "Category": [_projection("Column", "hour_ist", active=True)],
                    "Series": [_projection("Column", "city_name")],
                    "Y": [_projection("Measure", "Mean PM2.5 (µg/m³)")],
                },
                5,
            ),
        ),
        (
            "PM25_Wind_Scatter",
            _visual(
                "pm25_wind_scatter",
                "scatterChart",
                806,
                224,
                450,
                472,
                {
                    "Details": [_projection("Column", "city_name", active=True)],
                    "X": [_projection("Column", "wind_speed_kmh")],
                    "Y": [_projection("Measure", "Mean PM2.5 (µg/m³)")],
                },
                6,
            ),
        ),
    ]
    patterns = slicers() + [
        (
            "Hour_Weekday_Heatmap",
            _visual(
                "pm25_weekday_hour_matrix",
                "pivotTable",
                24,
                116,
                1232,
                580,
                {
                    "Rows": [_projection("Column", "local_weekday", active=True)],
                    "Columns": [_projection("Column", "local_hour", active=True)],
                    "Values": [_projection("Measure", "Mean PM2.5 (µg/m³)")],
                },
                2,
            ),
        )
    ]
    page_ids = [
        _page(report_dir, "Overview", "Overview", overview),
        _page(report_dir, "HourlyPatterns", "Hourly patterns", patterns),
    ]
    _write_json(
        definition_dir / "pages" / "pages.json",
        {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "report/definition/pagesMetadata/1.0.0/schema.json"
            ),
            "pageOrder": page_ids,
            "activePageName": page_ids[0],
        },
    )
    (project_dir / ".gitignore").write_text(
        "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n",
        encoding="utf-8",
    )
    return project_dir / f"{PROJECT_NAME}.pbip"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the AeroSense Power BI Desktop project."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT,
        help="Output directory (defaults to this folder).",
    )
    args = parser.parse_args()
    print(build_dashboard(args.output.resolve()))


if __name__ == "__main__":
    main()
