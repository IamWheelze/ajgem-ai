"""Minimal OGC GeoPackage 1.4 writer for point layers (FR20 P1).

Writes the required metadata tables (``gpkg_spatial_ref_sys``, ``gpkg_contents``,
``gpkg_geometry_columns``) and one feature table per layer, with geometries in the standard
GeoPackage binary encoding (``GP`` header plus little-endian WKB). There is no dependency on GDAL.

Every attribute is stored as TEXT. Assay values, detection limits and coordinates are exact
decimals in this system and must not pass through floating point on their way out (FRD design
rule); the point geometry itself is necessarily binary double precision, and the exact text
coordinates are kept alongside it.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone

WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]'
)
APPLICATION_ID = 0x47504B47  # "GPKG"
USER_VERSION = 10400  # GeoPackage 1.4.0


def point_blob(lon: float, lat: float, srs_id: int = 4326) -> bytes:
    """GeoPackage binary: magic, version 0, flags (little-endian, no envelope), srs id, then WKB Point."""
    header = b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", srs_id)
    return header + struct.pack("<BIdd", 1, 1, lon, lat)


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def write(layers: list[dict]) -> bytes:
    """layers: [{"name", "description", "columns": [...], "rows": [dict with lon/lat (str|None) and columns]}]."""
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        try:
            con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            con.execute(f"PRAGMA user_version = {USER_VERSION}")
            con.executescript("""
                CREATE TABLE gpkg_spatial_ref_sys (
                  srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY, organization TEXT NOT NULL,
                  organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
                CREATE TABLE gpkg_contents (
                  table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
                  description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                  min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
                  CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
                CREATE TABLE gpkg_geometry_columns (
                  table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
                  srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
                  CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
                  CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
                  CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
            """)
            con.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
                ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
                ("WGS 84 geodetic", 4326, "EPSG", 4326, WGS84_WKT, "longitude/latitude coordinates in decimal degrees"),
            ])
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            for layer in layers:
                t = _ident(layer["name"])
                cols = layer["columns"]
                col_sql = ", ".join(f"{_ident(c)} TEXT" for c in cols)
                con.execute(f"CREATE TABLE {t} (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT, {col_sql})")
                xs, ys = [], []
                rows = []
                for r in layer["rows"]:
                    geom = None
                    if r.get("lon") is not None and r.get("lat") is not None:
                        x, y = float(r["lon"]), float(r["lat"])
                        xs.append(x)
                        ys.append(y)
                        geom = point_blob(x, y)
                    rows.append([geom] + [None if r.get(c) is None else str(r.get(c)) for c in cols])
                placeholders = ", ".join("?" for _ in range(len(cols) + 1))
                names = ", ".join(["geom"] + [_ident(c) for c in cols])
                con.executemany(f"INSERT INTO {t} ({names}) VALUES ({placeholders})", rows)
                con.execute("INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    layer["name"], "features", layer["name"], layer.get("description", ""), now,
                    min(xs) if xs else None, min(ys) if ys else None, max(xs) if xs else None, max(ys) if ys else None,
                    4326))
                con.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
                            (layer["name"], "geom", "POINT", 4326, 0, 0))
            con.commit()
        finally:
            con.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)
