#!/usr/bin/env python3
# providers -- what is OUT THERE, and how to go get it. The librarian half.
#
# The registry answers "what does kingfisher know about" (cheap, no flag);
# refresh_index() and fetch() reach somebody else's servers and are
# flipr-gated from birth: fetch.enabled AND fetch.<source_id>, defaults off.
# An unreachable flipr means FliprDown means the fetch REFUSES -- fail-closed
# is the designed behaviour, not an error state.
#
# ENDPOINT FACTS ONLY from nico579/lidar2map (GPL-3.0): that repository was
# read as a map of provider APIs, and no code was copied from it -- GPL text
# in this tree would make kingfisher a derivative work, and distribution is
# on the roadmap. The implementations here are original.
#
# A provider entry is data, not a subclass. Adding one is adding a dict.

import json
import os
import queue
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import net
from log import log

USER_AGENT = "kingfisher/1.0 (map data librarian)"

# The outbound door moved to net.py on 2026-09-01, and the reason it was here
# is the reason it is there: injectable so the suite can stub the network
# without monkeypatching urllib for every OTHER caller in the process --
# urllib.request is a shared module object, and patching it globally hijacks
# the test harness's own HTTP client. Now there is ONE such door for the whole
# service rather than one per module, and it comes with backoff attached.

# id doubles as the flipr flag suffix: fetch.<id>. TAXONOMY owns the nouns.
SOURCES = [
    {"id": "usgs", "title": "USGS 3DEP / The National Map",
     "index_url": "https://tnmaccess.nationalmap.gov/api/v1/products",
     "expensive": True, "paid": False, "country": "us"},
    {"id": "noaa", "title": "NOAA InPort",
     "index_url": "https://www.fisheries.noaa.gov/inport/",
     "expensive": True, "paid": False},
    {"id": "overture", "title": "Overture Maps (buildings, places)",
     "index_url": "https://overturemaps.org/download/",
     "expensive": True, "paid": False},
    {"id": "nasa_gibs", "title": "NASA GIBS (satellite imagery, no auth)",
     # THE easy NASA door, for "their interface is really hard to navigate":
     # GIBS serves 1000+ layers (VIIRS/MODIS true color daily, Black Marble,
     # fires, aerosol, snow) as clean WMTS/XYZ tiles, and the Worldview
     # Snapshots API (wvs.earthdata.nasa.gov) hands back a plain image for
     # bbox+date+layer with NO AUTH. Earthdata login is only for bulk raw
     # granules -- the pretty pictures are the free tier.
     "index_url": "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/wmts.cgi?SERVICE=WMTS&REQUEST=GetCapabilities",
     "expensive": True, "paid": False},
    {"id": "nasa", "title": "NASA Black Marble night lights",
     "index_url": "https://blackmarble.gsfc.nasa.gov/",
     "expensive": True},
    {"id": "carto", "title": "Carto basemap",
     # the one PAID source: an api key with a bill behind it
     # (op: carto-basemap-api-key)
     "index_url": "https://carto.com/basemaps/",
     "expensive": True, "paid": True},
    {"id": "asf", "title": "ASF (NASA SAR archive: UAVSAR, Sentinel-1 InSAR)",
     # the "linear interferometry images that are hard to find": ASF's
     # search API is the findable door to JPL's interferograms -- bbox
     # queryable JSON, and the BROWSE images (the fringe maps themselves)
     # need no Earthdata login. Full granule downloads do; the adapter
     # indexes browse-image URLs, which is what a human wants anyway.
     "index_url": "https://api.daac.asf.alaska.edu/services/search/param",
     "expensive": True, "paid": False},
    {"id": "data_gov", "title": "data.gov (US federal open data, CKAN)",
     # "all those data.gov sets." A CKAN catalog -- when the adapter
     # lands, RefreshIndex should take a SEARCH TERM and index narrowly;
     # this catalog is a firehose and nobody wants all of it at once.
     "index_url": "https://catalog.data.gov/api/3/action/package_search",
     "expensive": True, "paid": False, "country": "us"},
    {"id": "opensky", "title": "OpenSky Network (live ADS-B flight state)",
     # real-time flight data, genuinely free (Swiss
     # research project, not a commercial reseller behind a "free tier").
     # /states/all needs no auth at all; verified LIVE (149 aircraft over a
     # Bay Area bbox, 2026-08-28). Registered/OAuth2 credentials raise the
     # ceiling (400 credits/day anonymous -> 4000/day registered, 10s -> 5s
     # resolution) but are not required to start.
     #
     # DOES NOT FIT refresh_index/start_fetch AS WRITTEN, and should not be
     # forced to: every other source here is a CATALOG of discrete,
     # downloadable products (a granule, a DEM tile) with a fixed vintage.
     # OpenSky has none of that -- one endpoint, one constantly-changing
     # snapshot, no "download_url" pointing at a stable file. our own
     # framing is the right one: "it's a trace like at least some of our
     # other data" -- the product here is not a dataset to fetch once, it
     # is a per-aircraft (icao24) TRAJECTORY assembled by POLLING and
     # accumulating state vectors over a window, the same shape as a GPS
     # ride track or a traceroute's hop sequence, just sourced from a live
     # feed instead of a single request.
     #
     # So the adapter this needs is closer to ingestd's periodic-daemon
     # shape than to refresh_index's catalog-and-fetch-one shape: poll
     # /states/all on a bbox at a cadence that respects the credit ceiling
     # (a bbox query's credit cost scales with its area -- size the poll
     # window and cadence together, not separately), accumulate each
     # icao24's positions into a trace, and let a trace go cold (no
     # last_contact update past some threshold) the way a ride ends.
     # Display is explicitly NOT decided here ("how we render that
     # is up to us") -- this is the fetch/accumulation shape only.
     #
     # The poller landed 2026-08-28 as openskyd.py -- ingestd's shape, its
     # own pod/port, gated by fetch.opensky. It keeps the trace in memory
     # (inherently ephemeral; a restart costs one empty sky for one poll
     # interval, not a durability incident) and writes an atomic snapshot
     # into a mount the serving pod reads read-only, same as ingestd's OUT
     # dir. This entry stays registered so the source is honest about what
     # it is even though its shape never fits refresh_index/start_fetch.
     "index_url": "https://opensky-network.org/api/states/all",
     "expensive": True, "paid": False},
    {"id": "weather", "title": "Open-Meteo + NWS (current conditions, redundant)",
     # "let's just make an adapter for both because
     # sometimes providers don't like us." Same poller shape as opensky --
     # not a catalog, a live snapshot per point, polled and kept in memory.
     # weatherd.py tries Open-Meteo first (global, no auth, generous free
     # tier) and falls back to NWS/api.weather.gov (US-only, genuinely no
     # rate limit, but a government API whose reliability varies) only when
     # Open-Meteo's call for that point fails. Both verified live 2026-08-28
     # with zero auth. Gated by the single fetch.weather flag -- which
     # provider actually answered a given point is recorded per-reading,
     # not a configuration choice.
     "index_url": "https://api.open-meteo.com/v1/forecast",
     "expensive": True, "paid": False},
    # ------------------------------------------------------------------
    # the national open-lidar programs, 54 sources across 27 countries.
    # ENDPOINT FACTS extracted from nico579/lidar2map (GPL-3.0 -- read as
    # a phone book, no code copied; us_tnm omitted as a duplicate of the
    # usgs row above, *_laz variants folded as format details). All free,
    # all large: expensive means bandwidth, never money. Each id is its
    # future flipr flag suffix; flags publish when adapters land, per the
    # wired-flags-only rule.
    {"id": 'at_bev', "title": 'Austria: Bev lidar (open data)',
     "index_url": 'https://data.bev.gv.at/geonetwork/srv/atom/describe/service',
     "expensive": True, "paid": False, "country": 'at'},
    {"id": 'at_osttirol', "title": 'Austria: Osttirol lidar (open data)',
     "index_url": 'https://www.tirol.gv.at/sicherheit/geoinformation/geodaten-tiris/laserscandaten/',
     "expensive": True, "paid": False, "country": 'at'},
    {"id": 'at_tirol', "title": 'Austria: Tirol lidar (open data)',
     "index_url": 'https://www.tirol.gv.at/sicherheit/geoinformation/geodaten-tiris/laserscandaten/',
     "expensive": True, "paid": False, "country": 'at'},
    {"id": 'au_ga', "title": 'Australia: Ga lidar (open data)',
     "index_url": 'https://ecat.ga.gov.au/geonetwork/srv/eng/catalog.search#/metadata/22be4b55-2466-4320-e053-10a3070a5236',
     "expensive": True, "paid": False, "country": 'au'},
    {"id": 'au_nsw', "title": 'Australia: Nsw lidar (open data)',
     "index_url": 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_5M_Elevation/ImageServer',
     "expensive": True, "paid": False, "country": 'au'},
    {"id": 'au_qld', "title": 'Australia: Qld lidar (open data)',
     "index_url": 'https://spatial-img.information.qld.gov.au/arcgis/rest/services/Elevation/QldDem/ImageServer',
     "expensive": True, "paid": False, "country": 'au'},
    {"id": 'be_flanders', "title": 'Belgium: Flanders lidar (open data)',
     "index_url": 'https://geo.api.vlaanderen.be/dhmv/wcs',
     "expensive": True, "paid": False, "country": 'be'},
    {"id": 'ca_nrcan', "title": 'Canada: Nrcan lidar (open data)',
     "index_url": 'https://datacube.services.geo.ca/stac/api/search',
     "expensive": True, "paid": False, "country": 'ca'},
    {"id": 'ca_quebec', "title": 'Canada: Quebec lidar (open data)',
     "index_url": 'https://www.donneesquebec.ca/recherche/dataset/produits-derives-de-base-du-lidar',
     "expensive": True, "paid": False, "country": 'ca'},
    {"id": 'ch_swisstopo', "title": 'Switzerland: Swisstopo lidar (open data)',
     "index_url": 'https://www.swisstopo.admin.ch/en/height-model-swissalti3d',
     "expensive": True, "paid": False, "country": 'ch'},
    {"id": 'cz_cuzk', "title": 'Czechia: Cuzk lidar (open data)',
     "index_url": 'https://ags.cuzk.cz/geoprohlizec/?atom=dm5g',
     "expensive": True, "paid": False, "country": 'cz'},
    {"id": 'de_bayern', "title": 'Germany: Bayern lidar (open data)',
     "index_url": 'https://geodaten.bayern.de/opengeodata/OpenDataDetail.html?pn=dgm1',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_berlin', "title": 'Germany: Berlin lidar (open data)',
     "index_url": 'https://gdi.berlin.de/data/dgm1/atom',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_brandenburg', "title": 'Germany: Brandenburg lidar (open data)',
     "index_url": 'https://inspire.brandenburg.de/services/el_dgm1_wcs?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCapabilities',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_bw', "title": 'Germany: Bw lidar (open data)',
     "index_url": 'https://owsproxy.lgl-bw.de/owsproxy/wcs/WCS_INSP_BW_Hoehe_Coverage_DGM1?REQUEST=GetCapabilities&SERVICE=WCS&version=2.0.1',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_hessen', "title": 'Germany: Hessen lidar (open data)',
     "index_url": 'https://inspire-hessen.de/raster/dgm1/ows?REQUEST=GetCapabilities&SERVICE=WCS&VERSION=2.0.1',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_mv', "title": 'Germany: Mv lidar (open data)',
     "index_url": 'https://www.geodaten-mv.de/dienste/inspire_el_dgm_wcs?REQUEST=GetCapabilities&SERVICE=WCS&VERSION=2.0.1',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_niedersachsen', "title": 'Germany: Niedersachsen lidar (open data)',
     "index_url": 'https://opengeodata.lgln.niedersachsen.de/',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_nrw', "title": 'Germany: Nrw lidar (open data)',
     "index_url": 'https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_rlp', "title": 'Germany: Rlp lidar (open data)',
     "index_url": 'https://geoshop.rlp.de/opendata-dgm1.html',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_sh', "title": 'Germany: Sh lidar (open data)',
     "index_url": 'https://www.schleswig-holstein.de/DE/landesregierung/ministerien-behoerden/LVERMGEOSH/Service/serviceGeobasisdaten/geodatenService_Geobasisdaten_DGM',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_st', "title": 'Germany: St lidar (open data)',
     "index_url": 'https://geodatenportal.sachsen-anhalt.de/ows_INSPIRE_LVermGeo_ATKIS_EL_DGM_WCS?REQUEST=GetCapabilities&SERVICE=WCS&VERSION=2.0.1',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'de_thueringen', "title": 'Germany: Thueringen lidar (open data)',
     "index_url": 'https://geoportal.geoportal-th.de/dienste/atom_th_hoehendaten_dgm',
     "expensive": True, "paid": False, "country": 'de'},
    {"id": 'dk_datafordeler', "title": 'Denmark: Datafordeler lidar (open data)',
     "index_url": 'https://dataforsyningen.dk/data/4462',
     "expensive": True, "paid": False, "country": 'dk'},
    {"id": 'ee_maaamet', "title": 'Estonia: Maaamet lidar (open data)',
     "index_url": 'https://geoportaal.maaamet.ee/',
     "expensive": True, "paid": False, "country": 'ee'},
    {"id": 'es_cnig', "title": 'Spain: Cnig lidar (open data)',
     "index_url": 'https://servicios.idee.es/wcs-inspire/mdt?request=GetCapabilities&service=WCS',
     "expensive": True, "paid": False, "country": 'es'},
    {"id": 'es_euskadi', "title": 'Spain: Euskadi lidar (open data)',
     "index_url": 'https://www.geo.euskadi.eus/geoeuskadi/services/U11/WCS_KARTOGRAFIA/MapServer/WCSServer?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCapabilities',
     "expensive": True, "paid": False, "country": 'es'},
    {"id": 'es_icgc', "title": 'Spain: Icgc lidar (open data)',
     "index_url": 'https://www.icgc.cat/en/Geoinformation-and-Maps/Data-and-products/Digital-twins-Elevations',
     "expensive": True, "paid": False, "country": 'es'},
    {"id": 'es_navarra', "title": 'Spain: Navarra lidar (open data)',
     "index_url": 'https://www.navarra.es/es/web/geoportal/idena/servicios',
     "expensive": True, "paid": False, "country": 'es'},
    {"id": 'fi_maanmittauslaitos', "title": 'Finland: Maanmittauslaitos lidar (open data)',
     "index_url": 'https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje',
     "expensive": True, "paid": False, "country": 'fi'},
    {"id": 'fr_craig', "title": 'France: Craig lidar (open data)',
     "index_url": 'https://www.craig.fr/contenu/nuages-de-points-lidar',
     "expensive": True, "paid": False, "country": 'fr'},
    {"id": 'fr_guadeloupe', "title": 'France: Guadeloupe lidar (open data)',
     "index_url": 'https://geoservices.ign.fr/lidarhd',
     "expensive": True, "paid": False, "country": 'fr'},
    {"id": 'fr_ign', "title": 'France: Ign lidar (open data)',
     "index_url": 'https://data.geopf.fr/wfs/ows',
     "expensive": True, "paid": False, "country": 'fr'},
    {"id": 'fr_reunion', "title": 'France: Reunion lidar (open data)',
     "index_url": 'https://geoservices.ign.fr/lidarhd',
     "expensive": True, "paid": False, "country": 'fr'},
    {"id": 'gb_england', "title": 'Great Britain: England lidar (open data)',
     "index_url": 'https://environment.data.gov.uk/dataset/13787b9a-26a4-4775-8523-806d13af58fc',
     "expensive": True, "paid": False, "country": 'gb'},
    {"id": 'gb_scotland', "title": 'Great Britain: Scotland lidar (open data)',
     "index_url": 'https://registry.opendata.aws/scottish-lidar/',
     "expensive": True, "paid": False, "country": 'gb'},
    {"id": 'gb_wales', "title": 'Great Britain: Wales lidar (open data)',
     "index_url": 'https://datamap.gov.wales/maps/lidar-data-download/',
     "expensive": True, "paid": False, "country": 'gb'},
    {"id": 'ie_gsi', "title": 'Ireland: Gsi lidar (open data)',
     "index_url": 'https://gsi.geodata.gov.ie/server/rest/services/Lidar',
     "expensive": True, "paid": False, "country": 'ie'},
    {"id": 'it_emilia_romagna', "title": 'Italy: Emilia Romagna lidar (open data)',
     "index_url": 'https://geoportale.regione.emilia-romagna.it/catalogo/dati-cartografici/altimetria/layer-2',
     "expensive": True, "paid": False, "country": 'it'},
    {"id": 'it_piemonte', "title": 'Italy: Piemonte lidar (open data)',
     "index_url": 'https://www.geoportale.piemonte.it/geonetwork/srv/api/records/r_piemon:224de2ac-023e-441c-9ae0-ea493b217a8e',
     "expensive": True, "paid": False, "country": 'it'},
    {"id": 'it_sardegna', "title": 'Italy: Sardegna lidar (open data)',
     "index_url": 'https://www.sardegnageoportale.it/webgis/',
     "expensive": True, "paid": False, "country": 'it'},
    {"id": 'jp_gsi', "title": 'Japan: Gsi lidar (open data)',
     "index_url": 'https://maps.gsi.go.jp/development/ichiran.html#dem',
     "expensive": True, "paid": False, "country": 'jp'},
    {"id": 'lu_act', "title": 'Luxembourg: Act lidar (open data)',
     "index_url": 'https://download.data.public.lu/resources/',
     "expensive": True, "paid": False, "country": 'lu'},
    {"id": 'lv_lgia', "title": 'Latvia: Lgia lidar (open data)',
     "index_url": 'https://s3.storage.pub.lvdc.gov.lv/lgia-opendata/las/LGIA_OpenData_las_saites.txt',
     "expensive": True, "paid": False, "country": 'lv'},
    {"id": 'nl_ahn', "title": 'Netherlands: Ahn lidar (open data)',
     "index_url": 'https://.../atom/downloads/dtm_05m/M_31DN2.tif',
     "expensive": True, "paid": False, "country": 'nl'},
    {"id": 'no_kartverket', "title": 'Norway: Kartverket lidar (open data)',
     "index_url": 'https://hoydedata.no/',
     "expensive": True, "paid": False, "country": 'no'},
    {"id": 'nz_linz', "title": 'New Zealand: Linz lidar (open data)',
     "index_url": 'https://registry.opendata.aws/nz-elevation/',
     "expensive": True, "paid": False, "country": 'nz'},
    {"id": 'ph_taal', "title": 'Philippines: Taal lidar (open data)',
     "index_url": 'https://phillidar-dad.github.io/taal-open-lidar.html',
     "expensive": True, "paid": False, "country": 'ph'},
    {"id": 'pl_gugik', "title": 'Poland: Gugik lidar (open data)',
     "index_url": 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF?request=GetCapabilities&service=WCS',
     "expensive": True, "paid": False, "country": 'pl'},
    {"id": 'pt_dgt', "title": 'Portugal: Dgt lidar (open data)',
     "index_url": 'https://cdd.dgterritorio.gov.pt/dgt-fe/catalogos',
     "expensive": True, "paid": False, "country": 'pt'},
    {"id": 'se_lantmateriet', "title": 'Sweden: Lantmateriet lidar (open data)',
     "index_url": 'https://www.lantmateriet.se/sv/geodata/vara-produkter/produktlista/markhojdmodell-nedladdning/',
     "expensive": True, "paid": False, "country": 'se'},
    {"id": 'si_arso', "title": 'Slovenia: Arso lidar (open data)',
     "index_url": 'http://gis.arso.gov.si/evode/',
     "expensive": True, "paid": False, "country": 'si'},
    {"id": 'us_3dep', "title": 'United States: 3Dep lidar (open data)',
     "index_url": 'https://opentopography.org/news/api-access-usgs-3dep-rasters-now-available',
     "expensive": True, "paid": False, "country": 'us'},
    {"id": 'us_cnmi', "title": 'United States: Cnmi lidar (open data)',
     "index_url": 'https://www.fisheries.noaa.gov/inport/item/66821',
     "expensive": True, "paid": False, "country": 'us'},
]

# the dataset index: everything kingfisher KNOWS ABOUT, held or not. Keyed by
# dataset id. Guarded by one lock; readers copy.
#
# IT IS DURABLE NOW, and the comment this replaces explains why it had to
# become so. It said "in-memory v0 -- the durable index lands with the surreal
# catalogue scope". Surreal is gone, so that scope will never land, and what
# was left was an index that did not survive its own pod. Measured 2026-08-30:
# kingfisher had restarted five times and /discovery reported 64 sources and
# ZERO datasets. Every RefreshIndex anyone had ever run was gone, which is the
# whole reason "kingfisher never downloads anything" reads as a mystery -- the
# catalogue of what COULD be downloaded kept evaporating, silently, and an
# empty index is indistinguishable from an index nobody built.
#
# The file lives in the spool, which is already a hostPath chosen for exactly
# this property (see DESIGN.md, "the seam is the FILESYSTEM, deliberately").
# No store, no schema, no migration: the index is a cache of other people's
# catalogues and is rebuildable by re-running RefreshIndex, so the cheapest
# durable thing is the correct one.
_INDEX_LOCK = threading.Lock()
_INDEX = {}
# tickets are deliberately NOT persisted. A ticket names an in-flight fetch on
# a thread, and no thread survives a restart -- writing them down would let
# GetFetchStatus answer about work that is not happening. load_index() settles
# the same question for the datasets those tickets pointed at: see below.
_TICKETS = {}


def index_path():
    """Where the index lives. The spool by default -- it is already durable."""
    return os.environ.get("KINGFISHER_INDEX") or os.path.join(
        os.environ.get("KINGFISHER_SPOOL", "spool"), "index.json")


def _save_index_locked():
    # caller holds _INDEX_LOCK. tmp+rename because a torn index.json is worse
    # than no index.json: the first is a parse error at startup that looks
    # like corruption, the second is an empty catalogue that rebuilds.
    path = index_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=".index-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"datasets": list(_INDEX.values())}, f)
        os.replace(tmp, path)
    except OSError as e:
        # a read-only spool must not take the fetch down with it: the index
        # still works for this process, it just will not outlive it, and
        # saying so is more use than raising into a caller that cannot act.
        log("warn", "index_not_saved", path=path, err=str(e)[:200])


def load_index():
    """Read the index from disk. Called once at startup; safe if absent."""
    path = index_path()
    try:
        with open(path) as f:
            rows = json.load(f).get("datasets", [])
    except FileNotFoundError:
        log("info", "index_absent", path=path)
        return 0
    except (OSError, ValueError) as e:
        log("warn", "index_unreadable", path=path, err=str(e)[:200])
        return 0
    healed = 0
    with _INDEX_LOCK:
        for d in rows:
            # A dataset recorded as FETCHING was mid-download when the process
            # died. Nothing is downloading it now, so leaving the state would
            # make the CLI and the display layer wait forever on a thread that
            # does not exist. FAILED is the honest reading and it is
            # retryable; UNSPECIFIED would claim less than we know.
            if d.get("state") == "FETCH_STATE_FETCHING":
                d["state"] = "FETCH_STATE_FAILED"
                healed += 1
            _INDEX[d["id"]] = d
        _save_index_locked()
    log("info", "index_loaded", path=path, datasets=len(rows),
        interrupted_fetches=healed)
    return len(rows)

# THE FETCH QUEUE. One worker, one download at a time, unbounded backlog.
#
# What this replaces: start_fetch() spawned a bare daemon thread per call, so
# N clicks were N concurrent downloads. Against USGS that is N multi-hundred-MB
# transfers at once out of a pod with a memory limit, competing for the same
# link and the same politeness budget -- and the display could not tell the
# operator anything useful because every one of them was "fetching" while none
# of them finished. reading exactly that screen: "i think
# maybe it only allows one download at a time? perhaps the clicks should end
# in a queue." It did not, and they should.
#
# Serial is also what DESIGN.md already ruled for this class of work ("the
# display layer asks over the RPC, and kingfisher runs the work IN-PROCESS, on
# a background thread, SERIALLY"). The bake path implements it; the fetch path
# never did. This is the fetch path catching up, not a new model.
#
# The backlog is unbounded ON PURPOSE. A bounded queue would have to refuse a
# click, and refusing is worse than waiting for work the operator explicitly
# asked for -- the queue depth is visible, so a long one is legible rather
# than surprising.
_FETCH_Q = queue.Queue()
_WORKER = None
_WORKER_LOCK = threading.Lock()


def _worker_loop():
    while True:
        dataset_id, spool_dir = _FETCH_Q.get()
        try:
            _run_fetch(dataset_id, spool_dir)
        except Exception as e:  # noqa: BLE001 -- the worker must outlive one bad fetch
            log("error", "fetch_worker_survived", dataset=dataset_id, err=str(e)[:200])
        finally:
            _FETCH_Q.task_done()


def _ensure_worker():
    # started on first use rather than at import: a test that never fetches
    # should not acquire a thread, and neither should a pod whose flags are off.
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, daemon=True,
                                       name="kingfisher-fetch")
            _WORKER.start()


def queue_depth():
    return _FETCH_Q.qsize()


# every OUTBOUND request counted, by source and outcome, plus bytes in.
# "how many requests are we making against somebody's servers" must be
# answerable from /metrics, not from their rate-limit email.
_OUT_LOCK = threading.Lock()
OUTBOUND = {}


def _count_out(source_id, outcome, nbytes=0):
    with _OUT_LOCK:
        row = OUTBOUND.setdefault(source_id, {})
        row[outcome] = row.get(outcome, 0) + 1
        row["bytes"] = row.get("bytes", 0) + nbytes


def list_sources():
    # `adapted` is computed, not stored: ADAPTED below is the single truth
    # about which sources have a working index adapter, and duplicating it
    # into 64 dicts is how the two drift. A display needs this -- without it
    # every source gets an identical button and 61 of them silently do
    # nothing, which is exactly the screen hit clicking
    # three unadapted lidar providers in a row.
    return [dict(s, adapted=s["id"] in ADAPTED) for s in SOURCES]


def list_datasets(source_id=None):
    with _INDEX_LOCK:
        rows = [dict(d) for d in _INDEX.values()]
    if source_id:
        rows = [d for d in rows if d["source_id"] == source_id]
    return sorted(rows, key=lambda d: d["id"])


def _get_json(source_id, url, timeout=30):
    # Accept-Encoding is EXPLICIT: some CDNs (worldview's included) compress
    # regardless of what you asked for, and an unnamed encoding read as
    # utf-8 is a 0x9d in position 0. Naming gzip pins the contract to one
    # encoding we decompress ourselves; stdlib urllib does not.
    #
    # Through net.py since 2026-09-01: backoff with full jitter, a per-attempt
    # deadline and a total budget. This is the index read, and it goes to
    # tnmaccess.nationalmap.gov and worldview among others -- the endpoints
    # flagged as "if we make them mad we will get banned". net honours
    # Retry-After, which is the specific difference between backing off and
    # arguing with a server that has already told us to wait.
    #
    # Decompression stays HERE. net is transport: it knows about attempts,
    # deadlines and status codes, and nothing about what a body means.
    try:
        _status, headers, raw = net.request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
            policy=net.DEFAULT.replace(timeout_s=timeout))
        enc = headers.get("Content-Encoding", "")
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        elif enc == "br":
            # worldview's CDN brotlis unconditionally; see the Dockerfile
            import brotli
            raw = brotli.decompress(raw)
        _count_out(source_id, "ok", len(raw))
        return json.loads(raw)
    except Exception:
        _count_out(source_id, "error")
        raise


def _require_flags(flags, source_id):
    # the gate, outermost first: network.enabled is the ONE switch that
    # stops every outbound request kingfisher makes ,
    # then the ratified fetch master AND per-source. Checked INLINE at the
    # top of the expensive act; FliprDown and FlagMissing propagate -- the
    # caller stops, loudly, by design.
    return (flags.check("network.enabled")
            and flags.check("fetch.enabled")
            and flags.check(f"fetch.{source_id}"))


# the sources with a working index adapter. Everything else in SOURCES is
# registered and honest about being unfetchable yet.
ADAPTED = {"usgs", "nasa_gibs", "asf"}


def refresh_index(source_id, flags, bbox=None, limit=50, query=""):
    # UNADAPTED answers first, before any flag consult: an unadapted
    # source's fetch.<id> flag is deliberately unpublished (wired-flags
    # only), and consulting it would turn an honest 501 into a FlagMissing
    # wiring error. The flag check guards the network call, and there is
    # no network call to guard.
    if source_id not in ADAPTED:
        return {"unimplemented": source_id}
    # querying a provider's catalogue is a network call on somebody else's
    # server, so it is gated exactly like a fetch, not like a listing.
    if not _require_flags(flags, source_id):
        return {"refused": f"fetch.enabled and fetch.{source_id} are not both on"}
    if source_id == "usgs":
        found = _usgs_products(bbox, limit)
    elif source_id == "nasa_gibs":
        found = _gibs_layers(bbox, limit, query)
    elif source_id == "asf":
        found = _asf_products(bbox, limit)
    with _INDEX_LOCK:
        for d in found:
            # WHAT WE ALREADY KNOW SURVIVES A RE-INDEX. The provider's
            # catalogue is authoritative about titles, sizes and urls; it knows
            # nothing about whether WE hold the bytes. Overwriting the record
            # wholesale reset a HELD dataset back to INDEXED, so the display
            # offered to download 363MB that was already on disk and already
            # hexed -- measured 2026-08-30, on exactly that tile.
            prior = _INDEX.get(d["id"])
            if prior:
                for k in ("state", "bytes_downloaded", "last_error",
                          "tombstone_reason", "tombstoned_at"):
                    if k in prior:
                        d[k] = prior[k]
            _INDEX[d["id"]] = d
        _save_index_locked()
    return {"indexed": len(found)}


def _usgs_products(bbox, limit):
    # TNM products API: plain JSON, no auth. datasets= narrows to lidar point
    # clouds; bbox is (w,s,e,n).
    q = {"datasets": "Lidar Point Cloud (LPC)", "max": str(limit),
         "outputFormat": "JSON"}
    if bbox:
        q["bbox"] = ",".join(str(x) for x in bbox)
    data = _get_json("usgs", "https://tnmaccess.nationalmap.gov/api/v1/products?"
                     + urllib.parse.urlencode(q))
    out = []
    for item in data.get("items", []):
        out.append({
            "id": "usgs-" + str(item.get("sourceId") or item.get("title", ""))[:80],
            "source_id": "usgs",
            "title": item.get("title", ""),
            # LayerKind: lidar elevation is INTENSIVE (a height is a height
            # at any resolution; folding averages, never sums)
            "kind": "LAYER_KIND_INTENSIVE",
            "bytes_estimate": int(item.get("sizeInBytes") or 0),
            "download_url": item.get("downloadURL", ""),
            "state": "FETCH_STATE_INDEXED",
            "vintage_id": str(item.get("publicationDate", ""))[:10] or "unknown",
        })
    return out


# the Worldview snapshot template: a plain image for layer+date+bbox, no
# auth. BBOX is EPSG:4326 miny,minx,maxy,maxx. This is the whole reason
# nasa_gibs is the easy door -- writing an Earthdata-login granule client
# is the pain declined; this is a URL.
_GIBS_SNAPSHOT = ("https://wvs.earthdata.nasa.gov/api/v1/snapshot"
                  "?REQUEST=GetSnapshot&LAYERS={layer}&CRS=EPSG:4326"
                  "&TIME={time}&BBOX={s},{w},{n},{e}"
                  "&FORMAT=image/jpeg&WIDTH=2048&HEIGHT=2048")


def _layer_date(meta, fallback):
    """A date this layer actually HAS, from its own declared ranges.

    The adapter used to ask every layer for yesterday. That is right for the
    daily imagery and wrong for everything else, and it fails in the worst
    way: NASA answers 200 with an empty frame, so a 2048x2048 request comes
    back as 28KB of black and nothing anywhere says why. Black Marble is
    `period: yearly` with data at 2012 and 2016 -- requested
    repeatedly and every fetch quietly produced nothing.

    dateRanges is authoritative and ordered, so the last range's start is the
    most recent vintage the layer genuinely holds.
    """
    ranges = meta.get("dateRanges") or []
    if ranges:
        d = ranges[-1].get("startDate") or ranges[-1].get("endDate")
        if d:
            return str(d)[:10]
    for key in ("endDate", "startDate"):
        if meta.get(key):
            return str(meta[key])[:10]
    return fallback


def _gibs_layers(bbox, limit, query=""):
    # the index: Worldview's own config names every GIBS layer with title
    # and period -- one GET, JSON, no XML capabilities wrestling.
    data = _get_json("nasa_gibs",
                     "https://worldview.earthdata.nasa.gov/config/wv.json",
                     timeout=60)
    w, s, e, n = bbox if bbox else (-122.6, 37.05, -121.55, 38.07)
    import datetime
    # yesterday: today's imagery is often not yet published
    day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    # FILTER BEFORE THE LIMIT, which is the whole bug. GIBS publishes roughly
    # a thousand WMTS layers and this took the first `limit` in dict order, so
    # every layer past the cut was unreachable however many times it was asked
    # for. Black Marble sat behind that line for weeks.
    q = (query or "").strip().lower()
    out = []
    for lid, meta in data.get("layers", {}).items():
        if len(out) >= limit:
            break
        if meta.get("type") != "wmts":
            continue
        title = meta.get("title", lid)
        if q and q not in lid.lower() and q not in str(title).lower():
            continue
        out.append({
            "id": "gibs-" + lid[:70],
            "source_id": "nasa_gibs",
            "title": title,
            # imagery is served and displayed, never folded: ASSET
            "kind": "LAYER_KIND_ASSET",
            "bytes_estimate": 0,   # a snapshot is made to order; no fixed size
            "download_url": _GIBS_SNAPSHOT.format(
                layer=lid, time=_layer_date(meta, day), w=w, s=s, e=e, n=n),
            "state": "FETCH_STATE_INDEXED",
            # the layer's OWN vintage, not the day we happened to ask on
            "vintage_id": _layer_date(meta, day),
        })
    return out


# UAVSAR granule names encode everything and read like line noise:
# UA_Haywrd_05502_18039-006_20024-029_0771d_s01_L090_01 is site Haywrd,
# pass pair 2018-day-039 vs 2020-day-024, 771-day baseline. a layer
# "should say what it is and have a simplified name" -- so the adapter
# decodes the name once, at the door, and every consumer downstream gets
# prose instead of an acronym to reverse-engineer.
_ASF_SITES = {"Haywrd": "Hayward fault", "SanAnd": "San Andreas fault",
              "SFBayD": "SF Bay Delta"}


def _asf_friendly(granule):
    import re
    m = re.match(r"UA_([A-Za-z]+)_\d+_(\d{2})(\d{3})-\d+_(\d{2})(\d{3})-\d+_(\d{4})d", granule)
    if not m:
        return f"UAVSAR interferogram {granule}"[:120], "SAR interferogram (UAVSAR L-band)."
    site = _ASF_SITES.get(m.group(1), m.group(1))
    y1, y2 = "20" + m.group(2), "20" + m.group(4)
    days = int(m.group(6))
    title = f"{site} ground deformation, {y1} to {y2} (UAVSAR)"
    desc = (f"Interferogram over the {site}: L-band radar pair {y1} to {y2}, "
            f"{days}-day baseline. Fringes are centimeter-scale surface motion "
            f"between the two passes.")
    return title, desc


def _asf_products(bbox, limit):
    # UAVSAR interferograms over the bbox, jsonlite shape. Products without
    # a browse image are SKIPPED, not indexed with an auth-walled url the
    # fetch gate would then fail on -- index only what fetch can honour.
    w, s, e, n = bbox if bbox else (-122.6, 37.05, -121.55, 38.07)
    q = urllib.parse.urlencode({
        "platform": "UAVSAR", "processingLevel": "INTERFEROMETRY",
        "bbox": f"{w},{s},{e},{n}", "maxResults": str(limit),
        "output": "jsonlite"})
    data = _get_json("asf", "https://api.daac.asf.alaska.edu/services/search/param?" + q,
                     timeout=60)
    out = []
    for item in data.get("results", []):
        browse = item.get("browse") or []
        if not browse:
            continue
        name = item.get("granuleName") or item.get("productName") or "scene"
        friendly, desc = _asf_friendly(name)
        out.append({
            "id": ("asf-" + name)[:80],
            "source_id": "asf",
            "title": friendly,
            "description": desc,
            "kind": "LAYER_KIND_ASSET",
            "bytes_estimate": int(float(item.get("sizeMB") or 0) * 1e6),
            "download_url": browse[0],
            "state": "FETCH_STATE_INDEXED",
            "vintage_id": str(item.get("startTime", ""))[:10],
        })
    return out


def start_fetch(dataset_id, flags, spool_dir):
    # the expensive act. Flag check FIRST, then a claim ticket; the download
    # runs on a thread and the ticket carries its state. v0 spools bytes to
    # disk; H3 ingestion is the compose work, deliberately not here.
    with _INDEX_LOCK:
        d = _INDEX.get(dataset_id)
    if d is None:
        return {"error": f"unknown dataset {dataset_id}"}
    # an operator's word outranks a flag, and costs no flipr call to honour
    if d.get("state") == "FETCH_STATE_TOMBSTONED":
        return {"refused": f"dataset {dataset_id} is tombstoned"
                           f" ({d.get('tombstoned_at', '')}): {d.get('tombstone_reason', '')}"}
    if not _require_flags(flags, d["source_id"]):
        return {"refused": f"fetch.enabled and fetch.{d['source_id']} are not both on"}
    if not d.get("download_url"):
        return {"error": f"dataset {dataset_id} carries no download url"}
    ticket = f"t{int(time.time()*1000):x}"
    with _INDEX_LOCK:
        # QUEUED, not FETCHING: nothing is downloading yet and saying otherwise
        # is what made a backlog look like a stall. The worker flips it.
        d["state"] = "FETCH_STATE_QUEUED"
        d["bytes_downloaded"] = 0
        d.pop("last_error", None)
        _TICKETS[ticket] = dataset_id
        _save_index_locked()
    # NO META YET. It used to be written here, before the download, so a crash
    # between the two would leave meta-without-payload rather than an orphan
    # payload. The cost (issue 79) was that EVERY download looked like an
    # ingest failure for as long as it ran: the scanner found the meta,
    # missed the payload, counted an error, and put the item into backoff --
    # so a long download appeared on the shelf up to an hour after it
    # finished, and its give-up clock ran from the start of the transfer.
    # The worker writes the meta after the bytes land; see _run_fetch.
    _ensure_worker()
    _FETCH_Q.put((dataset_id, spool_dir))
    return {"ticket": ticket, "queued_behind": max(0, _FETCH_Q.qsize() - 1)}


def _run_fetch(dataset_id, spool_dir):
    import os
    with _INDEX_LOCK:
        d = dict(_INDEX[dataset_id])
    dest = os.path.join(spool_dir, dataset_id)
    with _INDEX_LOCK:
        _INDEX[dataset_id]["state"] = "FETCH_STATE_FETCHING"
        _save_index_locked()
    try:
        os.makedirs(spool_dir, exist_ok=True)
        n = 0

        # published every megabyte, not every chunk: the display polls on a
        # human interval and a lock taken 1000x/second to serve a number nobody
        # reads that often is pure contention. net.download's chunk is 1MB, so
        # per-chunk IS per-megabyte.
        def _progress(so_far):
            with _INDEX_LOCK:
                _INDEX[dataset_id]["bytes_downloaded"] = so_far

        # net.DOWNLOAD, which is TWO attempts rather than the usual four. A
        # retried download is not a retried read: a 400MB tile restarted from
        # zero costs the provider the whole transfer again. the ceiling, for
        # exactly these endpoints: "the government's websites are pretty flaky
        # and a retry is important, but.. there's a limit." One restart is worth
        # it for a connection that dropped at 90%; a second is us being the
        # problem. net.download truncates on each attempt rather than appending,
        # so a retry cannot leave a corrupt file that reports success.
        # PAYLOAD FIRST, UNDER A NAME THE SCANNER DOES NOT READ. The bytes go
        # to <id>.part and are renamed into place only when complete, so a
        # crash mid-transfer leaves a file that says what it is rather than
        # one that looks whole. Then the meta -- itself via tmp+rename, so a
        # torn sidecar cannot be parsed. From ingestd's side this makes
        # "meta without payload" IMPOSSIBLE by this path, which is what lets
        # it treat that shape as broken and dead-letter it instead of backing
        # off forever (issue 79, both halves).
        part = dest + ".part"
        n = net.download(d["download_url"], part,
                         headers={"User-Agent": USER_AGENT},
                         on_progress=_progress)
        os.replace(part, dest)
        meta_tmp = os.path.join(spool_dir, dataset_id + ".meta.json.tmp")
        with open(meta_tmp, "w") as f:
            json.dump(d, f)
        os.replace(meta_tmp, os.path.join(spool_dir, dataset_id + ".meta.json"))
        _count_out(d["source_id"], "ok", n)
        state, err = "FETCH_STATE_HELD", None
        log("info", "fetch_held", dataset=dataset_id, bytes=n)
    except Exception as e:
        # nothing half-done stays behind: a partial .part is deleted, and no
        # meta was ever written, so the scanner has nothing to find
        try:
            os.remove(dest + ".part")
        except OSError:
            pass
        _count_out(d["source_id"], "error")
        state, err = "FETCH_STATE_FAILED", str(e)[:300]
        log("warn", "fetch_failed", dataset=dataset_id, err=str(e))
    with _INDEX_LOCK:
        _INDEX[dataset_id]["state"] = state
        _INDEX[dataset_id]["bytes_downloaded"] = n
        if err:
            _INDEX[dataset_id]["last_error"] = err
        else:
            _INDEX[dataset_id].pop("last_error", None)
        _save_index_locked()


# an operator's word, written into the index: never fetch this again, and why;
# or take the word back. Idempotent. The record keeps its title and url so the
# decision reads as a decision and can be reversed by flipping the state.
def tombstone(dataset_id, reason, revive=False):
    with _INDEX_LOCK:
        d = _INDEX.get(dataset_id)
        if d is None:
            return {"error": f"unknown dataset {dataset_id}"}
        if revive:
            d["state"] = "FETCH_STATE_INDEXED"
            d.pop("tombstone_reason", None)
            d.pop("tombstoned_at", None)
        else:
            d["state"] = "FETCH_STATE_TOMBSTONED"
            d["tombstone_reason"] = reason
            d["tombstoned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            d.pop("last_error", None)
        _save_index_locked()
        out = dict(d)
    log("info", "dataset_revived" if revive else "dataset_tombstoned",
        dataset=dataset_id, reason="" if revive else reason[:200])
    return out


def ticket_status(ticket):
    with _INDEX_LOCK:
        dataset_id = _TICKETS.get(ticket)
        if dataset_id is None:
            return None
        return dict(_INDEX[dataset_id])
