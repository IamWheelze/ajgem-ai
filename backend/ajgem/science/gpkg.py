"""Minimal OGC GeoPackage 1.4 writer for point layers (FR20 P1), and a reader for feature layers (FR23).

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
from datetime import UTC, datetime

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
    """Quote an identifier. Names come from the file itself, so quotes are escaped rather than trusted."""
    if not name or "\x00" in name:
        raise ValueError(f"unsafe identifier {name!r}")
    return '"' + name.replace('"', '""') + '"'


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
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
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


# ---------------------------------------------------------------- reading (FR23)
SQLITE_MAGIC = b"SQLite format 3\x00"
_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
_WKB_TYPES = {1: "POINT", 2: "LINESTRING", 3: "POLYGON", 4: "MULTIPOINT", 5: "MULTILINESTRING", 6: "MULTIPOLYGON",
              7: "GEOMETRYCOLLECTION"}


class GeoPackageError(ValueError):
    pass


def is_geopackage(data: bytes) -> bool:
    """SQLite header with the GeoPackage application id ("GPKG" or the 1.0 "GP10")."""
    return data.startswith(SQLITE_MAGIC) and len(data) >= 72 and data[68:72] in (b"GPKG", b"GP10", b"GP11")


def parse_geometry(blob: bytes | None) -> dict:
    """{"type", "x", "y"} for a point; {"type", "x": None, "y": None} for anything else or an empty geometry.

    Coordinates are returned as the exact decimal text of the stored doubles (repr), never rounded.
    """
    if blob is None:
        return {"type": None, "x": None, "y": None}
    if len(blob) < 8 or blob[:2] != b"GP":
        raise GeoPackageError("geometry is not GeoPackage binary")
    flags = blob[3]
    if flags & 0b00100000:
        raise GeoPackageError("extended GeoPackage geometry types are not supported")
    env = (flags >> 1) & 0b111
    if env not in _ENVELOPE_BYTES:
        raise GeoPackageError("invalid envelope indicator")
    empty = bool(flags & 0b00010000)
    wkb = blob[8 + _ENVELOPE_BYTES[env]:]
    if len(wkb) < 5:
        raise GeoPackageError("truncated WKB")
    endian = "<" if wkb[0] == 1 else ">"
    code = struct.unpack(endian + "I", wkb[1:5])[0]
    base = code % 1000 if code < 1_000_000 else code & 0xFF
    name = _WKB_TYPES.get(base, f"WKB_{code}")
    if base != 1 or empty:
        return {"type": name, "x": None, "y": None}
    x, y = struct.unpack(endian + "dd", wkb[5:21])
    if x != x or y != y:  # NaN: an empty point
        return {"type": "POINT", "x": None, "y": None}
    return {"type": "POINT", "x": repr(x), "y": repr(y)}


def _connect(path: str) -> sqlite3.Connection:
    # Untrusted file: read-only, immutable, and no SQL from the file's schema (views, triggers, defaults) is trusted.
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.execute("PRAGMA trusted_schema = OFF")
    con.execute("PRAGMA query_only = ON")
    return con


def layers(path: str) -> list[dict]:
    """Feature layers with their geometry column, geometry type and declared CRS."""
    con = _connect(path)
    try:
        try:
            rows = con.execute(
                "SELECT c.table_name, g.column_name, g.geometry_type_name, s.organization, s.organization_coordsys_id, "
                "s.srs_id FROM gpkg_contents c JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
                "LEFT JOIN gpkg_spatial_ref_sys s ON s.srs_id = g.srs_id WHERE c.data_type = 'features' "
                "ORDER BY c.table_name").fetchall()
        except sqlite3.DatabaseError as e:
            raise GeoPackageError(f"not a readable GeoPackage ({e})") from None
        out = []
        for table, geom_col, gtype, org, code, srs_id in rows:
            crs = f"{org.upper()}:{code}" if org and code is not None and srs_id not in (-1, 0) else None
            out.append({"name": table, "geometry_column": geom_col, "geometry_type": (gtype or "").upper(),
                        "crs": crs, "srs_id": srs_id})
        return out
    finally:
        con.close()


def read_layer(path: str, layer: dict, max_rows: int) -> tuple[list[str], list[tuple[int, dict]]]:
    """(attribute columns, [(fid, {column: text, "geometry": parsed})]) for one layer, in fid order."""
    con = _connect(path)
    try:
        t = _ident(layer["name"])
        info = con.execute(f"PRAGMA table_info({t})").fetchall()
        pk = next((c[1] for c in info if c[5] == 1), None)
        attrs = [c[1] for c in info if c[1] not in (pk, layer["geometry_column"])]
        cols = ", ".join([_ident(pk) if pk else "rowid", _ident(layer["geometry_column"])] + [_ident(a) for a in attrs])
        out = []
        for row in con.execute(f"SELECT {cols} FROM {t} ORDER BY 1 LIMIT ?", (max_rows + 1,)):
            fid, geom, *vals = row
            out.append((int(fid), {"geometry": geom, **dict(zip(attrs, vals, strict=True))}))
        return attrs, out
    finally:
        con.close()
