#!/usr/bin/env python3
"""
YouTube VOD (on-demand) Tracker -> Airtable

Sistema SEPARADO del tracker de streams en vivo (yt_tracker.py). Este mide
contenido on-demand (videos normales, nunca shorts) de canales u otras
playlists especificas.

Cada fuente activa en la tabla "VOD Sources" puede ser:
  - Source Type = Channel  -> se resuelve (y cachea) su playlist de uploads,
    y se trackean los uploads de ese canal.
  - Source Type = Playlist -> se trackean directamente los videos de esa
    playlist (Source ID = el Playlist ID).

En cada corrida (pensada para 1 vez por hora, sin ventana horaria: a
diferencia de los streams en vivo, las vistas de contenido on-demand pueden
crecer a cualquier hora del dia):
  1) Por cada fuente, trae los ULTIMOS 50 items de su pool de videos.
  2) Pide detalle (snippet, contentDetails, statistics) de esos videos.
  3) Descarta los que sean Shorts: se usa la heuristica estandar de duracion
     <= 60 segundos (YouTube no expone un campo oficial "esto es un Short" en
     la API publica, asi que esto es una aproximacion razonable, no 100%
     exacta).
  4) Para cada video no-short: crea/actualiza su registro en "VOD Videos" y
     agrega una fila en "VOD Snapshots" con la vista actual.

La consolidacion diaria (suscriptores + vistas acumuladas del dia + purga de
VOD Snapshots) vive en yt_vod_daily_consolidate.py, un script separado que
corre 1 vez por dia.

Requisitos:
  pip install requests

Variables de entorno requeridas (las mismas que yt_tracker.py / misma base):
  YOUTUBE_API_KEY
  AIRTABLE_TOKEN
"""

import os
import re
import sys
import datetime

import requests

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

BASE_ID = "appFvqj21Yy73Bjcn"

TBL_VOD_SOURCES = "tblINSjlF3geh6n83"
TBL_VOD_VIDEOS = "tblKIduNLgWQDEpNx"
TBL_VOD_SNAPSHOTS = "tbldk490qYDwR6Iny"

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()

MAX_VIDEOS_PER_SOURCE = 50          # "ultimos 50 videos" pedido por el usuario
SHORT_MAX_DURATION_SECONDS = 60     # heuristica: duracion <= 60s => Short (se descarta)

AIRTABLE_API = "https://api.airtable.com/v0"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

ISO8601_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not YOUTUBE_API_KEY:
    die("Falta la variable de entorno YOUTUBE_API_KEY")
if not AIRTABLE_TOKEN:
    die("Falta la variable de entorno AIRTABLE_TOKEN")


# ---------------------------------------------------------------------------
# Helpers Airtable
# ---------------------------------------------------------------------------

def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def airtable_list_all(table_id, filter_formula=None, fields=None):
    records = []
    params = {"pageSize": 100}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    if fields:
        params["fields[]"] = fields
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        resp = requests.get(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records


def airtable_create(table_id, records):
    created = []
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        resp = requests.post(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            json={"records": chunk, "typecast": True},
            timeout=30,
        )
        resp.raise_for_status()
        created.extend(resp.json().get("records", []))
    return created


def airtable_update(table_id, records):
    updated = []
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        resp = requests.patch(
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            json={"records": chunk, "typecast": True},
            timeout=30,
        )
        resp.raise_for_status()
        updated.extend(resp.json().get("records", []))
    return updated


# ---------------------------------------------------------------------------
# Helpers YouTube
# ---------------------------------------------------------------------------

def yt_get(path, **params):
    params["key"] = YOUTUBE_API_KEY
    resp = requests.get(f"{YOUTUBE_API}/{path}", params=params, timeout=30)
    if resp.status_code != 200:
        print(f"YouTube API error ({path}): {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def parse_iso8601_duration_to_seconds(duration_str):
    if not duration_str:
        return 0
    m = ISO8601_DURATION_RE.match(duration_str)
    if not m:
        return 0
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def get_uploads_playlist_id(channel_id):
    data = yt_get("channels", part="contentDetails", id=channel_id)
    items = data.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_recent_video_ids_for_playlist(playlist_id, max_items=MAX_VIDEOS_PER_SOURCE):
    """1 sola pagina (1 unidad de cuota): los items mas recientes de la playlist."""
    data = yt_get(
        "playlistItems",
        part="contentDetails",
        playlistId=playlist_id,
        maxResults=min(max_items, 50),
    )
    video_ids = []
    for item in data.get("items", []):
        vid = item["contentDetails"].get("videoId")
        if vid:
            video_ids.append(vid)
    return video_ids


def get_videos_details(video_ids):
    """Devuelve dict videoId -> {title, published_at, duration_sec, views}"""
    details = {}
    for batch in chunked(video_ids, 50):
        if not batch:
            continue
        data = yt_get(
            "videos",
            part="snippet,contentDetails,statistics",
            id=",".join(batch),
        )
        for item in data.get("items", []):
            vid = item["id"]
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})
            statistics = item.get("statistics", {})
            details[vid] = {
                "title": snippet.get("title", ""),
                "published_at": snippet.get("publishedAt"),
                "duration_sec": parse_iso8601_duration_to_seconds(content_details.get("duration")),
                "views": int(statistics.get("viewCount", 0)) if statistics.get("viewCount") is not None else None,
            }
    return details


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.datetime.utcnow()
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    sources = airtable_list_all(TBL_VOD_SOURCES, filter_formula="{Active}=1")
    if not sources:
        print("No hay fuentes VOD activas. Nada para hacer.")
        return

    existing_video_records = airtable_list_all(TBL_VOD_VIDEOS)
    existing_video_by_id = {
        r["fields"].get("Video ID", "").strip(): r
        for r in existing_video_records
        if r["fields"].get("Video ID")
    }

    source_updates = []
    video_creates = []
    video_updates = []
    snapshot_creates = []

    for source in sources:
        sid = source["id"]
        f = source["fields"]
        source_type = f.get("Source Type")
        source_id = (f.get("Source ID") or "").strip()
        name = f.get("Name", source_id)

        if not source_id:
            print(f"Aviso: fuente '{name}' sin Source ID, se saltea.", file=sys.stderr)
            continue

        playlist_id = None
        fields_update = {"Last Checked": now_iso}

        if source_type == "Channel":
            playlist_id = f.get("Uploads Playlist ID (cached)")
            if not playlist_id:
                try:
                    playlist_id = get_uploads_playlist_id(source_id)
                except requests.HTTPError as e:
                    print(f"Aviso: no se pudo resolver uploads playlist de '{name}': {e}", file=sys.stderr)
                    continue
                if playlist_id:
                    fields_update["Uploads Playlist ID (cached)"] = playlist_id
        elif source_type == "Playlist":
            playlist_id = source_id
        else:
            print(f"Aviso: fuente '{name}' con Source Type invalido ({source_type!r}), se saltea.", file=sys.stderr)
            continue

        if not playlist_id:
            print(f"Aviso: no se pudo determinar playlist para '{name}', se saltea.", file=sys.stderr)
            continue

        try:
            recent_ids = get_recent_video_ids_for_playlist(playlist_id)
        except requests.HTTPError as e:
            print(f"Aviso: error leyendo playlist de '{name}': {e}", file=sys.stderr)
            continue

        source_updates.append({"id": sid, "fields": fields_update})

        if not recent_ids:
            continue

        try:
            details = get_videos_details(recent_ids)
        except requests.HTTPError as e:
            print(f"Aviso: error leyendo detalle de videos de '{name}': {e}", file=sys.stderr)
            continue

        for vid in recent_ids:
            d = details.get(vid)
            if not d:
                continue
            is_short = d["duration_sec"] > 0 and d["duration_sec"] <= SHORT_MAX_DURATION_SECONDS
            if is_short:
                continue  # nunca trackear shorts

            existing = existing_video_by_id.get(vid)
            video_fields = {
                "Video ID": vid,
                "Title": d["title"],
                "URL": f"https://www.youtube.com/watch?v={vid}",
                "Source": [sid],
                "Duration (sec)": d["duration_sec"],
                "Is Short": False,
                "Current Views": d["views"],
                "Last Checked": now_iso,
            }
            if d.get("published_at"):
                video_fields["Published At"] = d["published_at"]

            if existing:
                video_updates.append({"id": existing["id"], "fields": video_fields})
                video_record_id = existing["id"]
            else:
                # Se crea ahora mismo (abajo, en bloque) para tener el record id
                # antes de poder escribir el snapshot; se resuelve en 2 pasadas
                # mas abajo para no complicar el batching.
                video_creates.append((vid, video_fields))
                video_record_id = None

            if d["views"] is not None:
                snapshot_creates.append((vid, video_record_id, d["views"]))

    # Crear primero los videos nuevos, para tener sus record ids
    vid_to_new_record_id = {}
    if video_creates:
        created = airtable_create(TBL_VOD_VIDEOS, [{"fields": vf} for _, vf in video_creates])
        for rec in created:
            vid = rec["fields"].get("Video ID")
            if vid:
                vid_to_new_record_id[vid] = rec["id"]

    if video_updates:
        airtable_update(TBL_VOD_VIDEOS, video_updates)

    # Ahora si, armar los snapshots con el record id correcto (nuevo o existente)
    snapshot_records_to_create = []
    for vid, video_record_id, views in snapshot_creates:
        rid = video_record_id or vid_to_new_record_id.get(vid)
        if not rid:
            continue
        snapshot_records_to_create.append(
            {"fields": {"Video": [rid], "Timestamp": now_iso, "Views": views}}
        )
    if snapshot_records_to_create:
        airtable_create(TBL_VOD_SNAPSHOTS, snapshot_records_to_create)

    if source_updates:
        airtable_update(TBL_VOD_SOURCES, source_updates)

    print(
        f"Listo. Fuentes procesadas: {len(source_updates)}, "
        f"videos nuevos: {len(video_creates)}, videos actualizados: {len(video_updates)}, "
        f"snapshots: {len(snapshot_records_to_create)}."
    )


if __name__ == "__main__":
    main()
