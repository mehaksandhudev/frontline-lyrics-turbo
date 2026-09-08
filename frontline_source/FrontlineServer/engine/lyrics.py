"""
Synced-lyrics lookup against the LRCLIB API (https://lrclib.net).

Optimized for:
1. Strict title + artist verification (eliminates false-positive foreign song matches).
2. Native script prioritization (prefers Gurmukhi/Devanagari over Romanized Latin).
3. Plain-lyrics fallback with automatic timeline distribution when synced lyrics are missing.
4. Persistent connection pooling for sub-second lookups.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests

_LRC_LINE = re.compile(r"\[(\d{2,}):(\d{2}(?:\.\d{1,3})?)\](.*)")
_PAREN_OR_BRACKET = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FEAT_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.I)
_DASH_SUFFIX = re.compile(
    r"\s+[\-–—]\s+(official.*|audio|video|lyric.*|from\s+.*|remaster.*|"
    r"live.*|radio\s+edit.*|slowed.*|sped\s+up.*)$",
    re.I,
)


def clean_lrclib_query(artist: str, song: str) -> Tuple[str, str]:
    """Strip noise from title/artist so it looks like what LRCLIB indexes."""
    raw_song = song or ""
    raw_artist = artist or ""
    cleaned_song = _PAREN_OR_BRACKET.sub("", raw_song)
    cleaned_song = _DASH_SUFFIX.sub("", cleaned_song)
    cleaned_song = re.sub(r"\s+", " ", cleaned_song).strip(" -")
    if not cleaned_song:
        cleaned_song = re.sub(r"\s+", " ", raw_song).strip()

    cleaned_artist = _FEAT_SPLIT.split(raw_artist)[0]
    cleaned_artist = cleaned_artist.split(",")[0].split("&")[0].split("/")[0].split(";")[0]
    cleaned_artist = _PAREN_OR_BRACKET.sub("", cleaned_artist)
    cleaned_artist = re.sub(r"\s+", " ", cleaned_artist).strip()
    if not cleaned_artist:
        cleaned_artist = re.sub(r"\s+", " ", raw_artist).strip()
    return cleaned_artist, cleaned_song


def normalize_text(text: str) -> str:
    """Normalize text for strict matching: lowercase, strip metadata and punctuation."""
    if not text:
        return ""
    t = text.lower().strip()
    t = _PAREN_OR_BRACKET.sub("", t)
    t = _DASH_SUFFIX.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def titles_match(query_title: str, candidate_title: str) -> bool:
    """Accurate title matching without false-positive substring collisions."""
    q = normalize_text(query_title)
    c = normalize_text(candidate_title)
    if not q or not c:
        return False
    if q == c:
        return True
    q_words = set(q.split())
    c_words = set(c.split())
    if q_words and q_words.issubset(c_words):
        return True
    if c_words and c_words.issubset(q_words):
        return True
    if len(q) >= 3 and (c.startswith(q + " ") or q.startswith(c + " ")):
        return True
    return False


def artists_match(query_artist: str, candidate_artist: str) -> bool:
    """Verify that the candidate track belongs to the requested artist."""
    q = normalize_text(query_artist)
    c = normalize_text(candidate_artist)
    if not q or not c:
        return False
    if q == c:
        return True
    q_tokens = [w for w in q.split() if len(w) > 2]
    c_tokens = [w for w in c.split() if len(w) > 2]
    if not q_tokens or not c_tokens:
        return q == c
    for qt in q_tokens:
        if qt in c:
            return True
    for ct in c_tokens:
        if ct in q:
            return True
    return False


def names_are_close(a: str, b: str) -> bool:
    """Backward compatibility alias for titles_match."""
    return titles_match(a, b)


def contains_native_indic_script(text: str) -> bool:
    """Returns True if text contains native Indic scripts (Gurmukhi \\u0A00-\\u0A7F, Devanagari \\u0900-\\u097F)."""
    if not text:
        return False
    for char in text:
        code = ord(char)
        if 0x0900 <= code <= 0x0D7F:
            return True
    return False


def parse_synced_lrc(synced_lyrics: str) -> List[Dict[str, Any]]:
    """Turn an LRC-format blob into a list of {timestamp, text} lines."""
    lines: List[Dict[str, Any]] = []
    for line in (synced_lyrics or "").split("\n"):
        match = _LRC_LINE.match(line)
        if not match:
            continue
        timestamp = (int(match.group(1)) * 60) + float(match.group(2))
        text = match.group(3).strip()
        if text:
            lines.append({"timestamp": timestamp, "text": text})
    if lines:
        lines.append({"timestamp": lines[-1]["timestamp"] + 5.0, "text": "End"})
    return lines


def parse_plain_lyrics_timed(plain_lyrics: str, estimated_duration: float = 190.0) -> List[Dict[str, Any]]:
    """Synthesizes smooth timeline pacing for unsynced plain lyrics when synced LRC is unavailable."""
    raw_lines = [line.strip() for line in (plain_lyrics or "").split("\n") if line.strip()]
    if not raw_lines:
        return []

    lines_text = [l for l in raw_lines if not (l.startswith("[") and l.endswith("]"))]
    if not lines_text:
        lines_text = raw_lines

    num_lines = len(lines_text)
    intro_lead = 8.0
    usable_duration = max(30.0, estimated_duration - intro_lead - 10.0)
    step = usable_duration / max(1, num_lines)
    step = max(2.5, min(6.0, step))

    timed_lines = []
    current_time = intro_lead
    for text in lines_text:
        timed_lines.append({"timestamp": round(current_time, 2), "text": text})
        current_time += step

    timed_lines.append({"timestamp": round(current_time + 4.0, 2), "text": "End"})
    return timed_lines


def score_candidate(item: Dict[str, Any], query_artist: str, query_song: str) -> int:
    """Score candidate result. Returns -1 if rejected."""
    if not isinstance(item, dict):
        return -1
    c_track = item.get("trackName") or ""
    c_artist = item.get("artistName") or ""

    if not titles_match(query_song, c_track):
        return -1
    if not artists_match(query_artist, c_artist):
        return -1

    score = 20
    synced = item.get("syncedLyrics")
    plain = item.get("plainLyrics")
    if synced:
        score += 20
        # Priority for native Indic scripts (Gurmukhi over Romanized transliteration)
        if contains_native_indic_script(synced):
            score += 25
    elif plain:
        score += 5
        if contains_native_indic_script(plain):
            score += 10
    else:
        return -1

    return score


def pick_lrclib_search_hit(results: Any, artist: str, song: str) -> Optional[Dict[str, Any]]:
    """Pick the best matching search result with strict artist/title verification and script prioritization."""
    if not isinstance(results, list):
        return None

    scored: List[Tuple[int, Dict[str, Any]]] = []
    for item in results:
        s = score_candidate(item, artist, song)
        if s > 0:
            scored.append((s, item))

    if not scored:
        return None

    scored.sort(key=lambda pair: -pair[0])
    return scored[0][1]


# ---------------------------------------------------------------------------
# Module-level persistent HTTP session — reuses TCP+TLS connections to
# lrclib.net across all lookups, saving ~300-800ms per call.
# ---------------------------------------------------------------------------
_persistent_session = None


def _get_session():
    global _persistent_session
    if _persistent_session is None:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        _persistent_session = requests.Session()
        retries = Retry(
            total=1,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=retries,
        )
        _persistent_session.mount("https://", adapter)
        _persistent_session.mount("http://", adapter)
        _persistent_session.headers.update(
            {"User-Agent": "FrontLineLyricsApp/1.3.0-turbo"}
        )
    return _persistent_session


def _lookup_get(session, track: str, who: str) -> Optional[Dict[str, Any]]:
    """Query /api/get with track_name + artist_name."""
    try:
        r = session.get(
            "https://lrclib.net/api/get",
            params={"track_name": track, "artist_name": who},
            timeout=4,
        )
        if r.status_code == 200:
            payload = r.json()
            if payload and isinstance(payload, dict):
                return payload
    except Exception as e:
        logging.warning(f"Exact match search failed (lrclib): {e}")
    return None


def _lookup_search(session, query: str) -> List[Dict[str, Any]]:
    """Broad search: /api/search with free-text query."""
    try:
        r = session.get(
            "https://lrclib.net/api/search",
            params={"q": query},
            timeout=4,
        )
        if r.status_code == 200:
            res = r.json()
            if isinstance(res, list):
                return res
    except Exception as e:
        logging.warning(f"Search failed (lrclib): {e}")
    return []


def fetch_lyrics_lrclib(artist: str, song: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch synced lyrics from LRCLIB API with smart script ranking and plain-lyrics fallback."""
    clean_artist, clean_song = clean_lrclib_query(artist, song)
    if not clean_song:
        return None

    session = _get_session()
    candidates: List[Dict[str, Any]] = []

    # Run /api/get and /api/search concurrently to get all candidate versions
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lrclib") as pool:
        f_get = pool.submit(_lookup_get, session, clean_song, clean_artist)
        query_str = f"{clean_song} {clean_artist}".strip()
        f_search = pool.submit(_lookup_search, session, query_str)

        try:
            get_res = f_get.result(timeout=4)
            if get_res:
                candidates.append(get_res)
        except Exception:
            pass

        try:
            search_res = f_search.result(timeout=4)
            if search_res:
                candidates.extend(search_res)
        except Exception:
            pass

    # Score all collected candidates against query
    scored: List[Tuple[int, Dict[str, Any]]] = []
    seen_ids = set()
    for item in candidates:
        item_id = item.get("id")
        if item_id and item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)

        s = score_candidate(item, clean_artist or artist, clean_song or song)
        if s > 0:
            scored.append((s, item))

    if not scored:
        logging.info(f"No valid lyric candidate matched {artist} - {song}")
        return None

    # Pick highest scoring candidate
    scored.sort(key=lambda pair: -pair[0])
    best = scored[0][1]

    # If best has syncedLyrics, parse as LRC
    if best.get("syncedLyrics"):
        lines = parse_synced_lrc(best["syncedLyrics"])
        if lines:
            return lines

    # Fallback: if only plainLyrics is available (e.g. Breeze - Bhalwaan)
    if best.get("plainLyrics"):
        duration = float(best.get("duration") or 190.0)
        lines = parse_plain_lyrics_timed(best["plainLyrics"], estimated_duration=duration)
        if lines:
            logging.info(f"Using paced plain-lyrics fallback for {artist} - {song}")
            return lines

    return None


def fetch_lyrics_from_candidates(pairs: List[Tuple[str, str]]) -> Optional[List[Dict[str, Any]]]:
    """Try several (artist, title) pairs until LRCLIB returns synced lyrics."""
    seen = set()
    for artist, song in pairs:
        key = ((artist or "").lower().strip(), (song or "").lower().strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        lines = fetch_lyrics_lrclib(artist, song)
        if lines:
            return lines
    return None
