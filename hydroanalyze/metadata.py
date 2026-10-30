from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field, asdict, fields
from logging import Logger
from pathlib import Path
from os import PathLike
import requests
from typing import ClassVar, Optional
import xml.etree.ElementTree as ET

from clients import NOAAClient

_REGISTRY: dict[str, type[Metadata]] = {}


XML_SCHEMA = 'http://www.cuahsi.org/waterML/1.1/'
XML_NAMESPACES = { # define namespace for parsing XML
    'wml': XML_SCHEMA, 
    'ns1': XML_SCHEMA
}

@dataclass
class Metadata:
    source: ClassVar[str] = "unknown"

    gage_id: str
    gage_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    fetched_at: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _REGISTRY[cls.source] = cls          # lets load() pick the right class

    # --- serialization ---
    def to_dict(self) -> dict:
        return {"source": self.source, **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> Metadata:
        d = dict(d)
        target = _REGISTRY.get(d.pop("source", None), Metadata)
        known = {f.name for f in fields(target)}

        return target(**{k: v for k, v in d.items() if k in known})


    # --- file I/O (the one place the filename is defined) ---
    @staticmethod
    def path_for(data_dir: str | PathLike, gage_id: str) -> Path:
        return Path(data_dir) / f"{gage_id}_meta.json"


    def get_fetch_time(self):
        self.fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()


    def save(self, data_dir: str | PathLike) -> Path:
        """
        Save metadata as a JSON file as data_dir/<gage_id>_meta.json
        """
        path = self.path_for(data_dir, self.gage_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=4, default=str),
            encoding="utf-8"
        )

        return path
    

    def pretty_print(self, all: Optional[bool]=False) -> str:
        
        fetched_date = dt.datetime.fromisoformat(self.fetched_at)\
            .strftime('%B %d, %Y %I:%M %p')

        if all:
            return json.dumps(self.as_dict(), indent=4)
        else:
            return (
                'Hydroanalyze Metadata\n'
                '~~~~~~~~~~~~~~~~~~~~~~~\n'
                f'Gage Name:    {self.gage_name}\n'
                f'Gage ID:      {self.gage_id}\n'
                f'Source:       {self.source}\n'
                f'Coordinates:  {self.latitude}, {self.longitude}\n'
                f'Time Zone:    {self.timezone}\n'
                f'As of:        {fetched_date}\n'
            )


    @classmethod
    def load(cls, data_dir: str | PathLike, gage_id: str) -> Metadata | None:
        path = cls.path_for(data_dir, gage_id)
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None


@dataclass
class USGSMetadata(Metadata):
    source: ClassVar[str] = "USGS"

    huc: str | None = None
    site_type: str | None = None
    state_code: str | None = None
    county_code: str | None = None
    variables: dict = field(default_factory=dict)

    @classmethod
    def from_xml(
            cls, 
            root: ET.Element, 
            gage_id:Optional[str]=None
        ) -> USGSMetadata:
        """
        Extract site, query, and variable metadata from a WaterML 1.1 response.

        :param xml.etree.ElementTree.Element root: Parsed root element
        :param optional str gage_id: Gage ID fallback if it's not in the xml
        :returns: USGSMetadata
        :rtype: dict
        """
        ns = XML_NAMESPACES  # matching is by URI, so ns1/ns2 prefixes both work
        raw = {"source": "USGS", "site": {}, "query": {}, "variables": {}}

        # --- Query info ---
        qi = root.find("ns1:queryInfo", ns)
        if qi is not None:
            raw["query"] = {
                "url": qi.findtext("ns1:queryURL", namespaces=ns),
                "begin": qi.findtext(".//ns1:beginDateTime", namespaces=ns),
                "end": qi.findtext(".//ns1:endDateTime", namespaces=ns),
                "notes": {
                    n.attrib.get("title"): (n.text or "").strip()
                    for n in qi.findall("ns1:note", ns)
                },
            }

        # --- Site info (identical across the series, so use the first) ---
        info = root.find(".//ns1:timeSeries/ns1:sourceInfo", ns)
        site = {} # instantiate in case info is None
        if info is not None:
            code_el = info.find("ns1:siteCode", ns)
            loc = info.find(".//ns1:geogLocation", ns)
            tz = info.find("ns1:timeZoneInfo", ns)
            std_tz = tz.find("ns1:defaultTimeZone", ns) if tz is not None else None
            dst_tz = tz.find("ns1:daylightSavingsTimeZone", ns) if tz is not None else None

            def _float(path):
                text = loc.findtext(path, namespaces=ns) if loc is not None else None
                return float(text) if text else None

            site = {
                "site_name": info.findtext("ns1:siteName", namespaces=ns),
                "site_code": code_el.text if code_el is not None else None,
                "agency": code_el.attrib.get("agencyCode") if code_el is not None else None,
                "network": code_el.attrib.get("network") if code_el is not None else None,
                "latitude": _float("ns1:latitude"),
                "longitude": _float("ns1:longitude"),
                "crs": loc.attrib.get("srs") if loc is not None else None,
                "uses_dst": (
                    tz.attrib.get("siteUsesDaylightSavingsTime") == "true"
                    if tz is not None else None
                ),
                "tz_standard": dict(std_tz.attrib) if std_tz is not None else None,
                "tz_daylight": dict(dst_tz.attrib) if dst_tz is not None else None,
                "properties": {
                    p.attrib.get("name"): p.text
                    for p in info.findall("ns1:siteProperty", ns)
                },
            }
            raw["site"] = site

        # --- Per-variable info ---
        for ts in root.findall("ns1:timeSeries", ns):
            var = ts.find("ns1:variable", ns)
            if var is None:
                continue
            code = var.findtext("ns1:variableCode", namespaces=ns)
            stat = var.findtext(".//ns1:option[@name='Statistic']", namespaces=ns)
            raw["variables"].setdefault(code, {}).update({
                "name": var.findtext("ns1:variableName", namespaces=ns),
                "description": var.findtext("ns1:variableDescription", namespaces=ns),
                "unit": var.findtext("ns1:unit/ns1:unitCode", namespaces=ns),
                "no_data_value": var.findtext("ns1:noDataValue", namespaces=ns),
            })
            raw["variables"][code].setdefault("statistics", [])
            if stat and stat not in raw["variables"][code]["statistics"]:
                raw["variables"][code]["statistics"].append(stat)
        
        site_properties = site['properties']
        meta = {
            "gage_name":    site.get("site_name"),
            "gage_id":      site.get("site_code") or gage_id,
            "latitude":     site.get("latitude"),
            "longitude":    site.get("longitude"),
            "timezone":     (site.get("tz_standard") or {}).get("zoneAbbreviation"),
            "state_code":   site_properties.get("stateCd"),
            "county_code":  site_properties.get("countyCd"),
            "site_type":    site_properties.get("siteTypeCd"),
            "huc":          site_properties.get("hucCd"),
            "variables":    raw.get("variables"),
            "raw":          raw,

        }
        
        metadata = cls(**meta)
        metadata.get_fetch_time()

        return metadata


@dataclass
class NOAAMetadata(Metadata):
    source: ClassVar[str] = "NOAA"

    usgs_id: str | None = None
    state: str | None = None

    @classmethod
    def from_api(cls, raw: dict, gage_id: str) -> NOAAMetadata:
        return cls(
            gage_id=gage_id,
            gage_name=raw.get("name"),
            latitude=raw.get("latitude"),
            longitude=raw.get("longitude"),
            usgs_id=raw.get("usgsId"),
            state=(raw.get("state") or {}).get("abbreviation"),
            timezone=raw.get("timeZone"),
            raw=raw,
        )


def ensure_noaa_metadata(
        gage_id:    str, 
        data_dir:   PathLike, 
        client:     Optional[NOAAClient]=None, 
        refresh:    Optional[bool]=False,
        log:        Optional[Logger]=None,
        write_file: Optional[bool]=True
    ) -> Metadata | None:
    """
    Make sure metadata exists. If it doesn't, try to load and save it
    """
    if not refresh:
        meta = Metadata.load(data_dir, gage_id)
        if meta is not None:
            return meta
    try:
        raw = (client or NOAAClient()).get_metadata(gage_id)
    except requests.RequestException as e:
        if log:
            log.warning('metadata request failed with error: %s', e)
        return Metadata(gage_id=gage_id)

    meta = NOAAMetadata.from_api(raw, gage_id)
    meta.get_fetch_time()
    
    # Write the data unless this is a dry run in ingest_noaa.py
    if write_file:
        meta.save(data_dir)
    
    return meta