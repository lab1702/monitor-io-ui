#!/usr/bin/env python3
"""NetMonitor dashboard server.

Pulls the CSV/log files published by a NetMonitor device (a lighttpd directory
listing), caches and parses them, and serves a JSON API plus the static
dashboard in ./static.  Standard library only.

    python3 server.py --device http://192.168.0.246/ --port 8080

CSV row layout (one row per probe cycle):

    Date,Time,Timezone,IPAddress, then repeated 7-column target blocks:
    Target,Transmit,Receive,LossPct,DelayMin,DelayAvg,DelayMax

When hostname targets cannot be resolved the device omits their blocks and
appends a block whose Target is "DNS:Failure(s)" (remaining 6 fields empty).
Rows can therefore have 32, 39, 46... columns, and a 46-column row can still
be a DNS failure.  The parser walks blocks rather than trusting column count.
"""

import argparse
import array
import bisect
import calendar
import copy
import csv
import io
import ipaddress
import json
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

NAN = float("nan")
TZ_OFFSETS = {  # hours east of UTC
    "UTC": 0, "GMT": 0, "Z": 0,
    "EDT": -4, "EST": -5, "CDT": -5, "CST": -6,
    "MDT": -6, "MST": -7, "PDT": -7, "PST": -8,
    "AKDT": -8, "AKST": -9, "HST": -10,
    "BST": 1, "CET": 1, "CEST": 2,
}
INDEX_TTL = 10          # seconds between directory-listing refreshes
GAP_SECONDS = 180       # sample spacing beyond this is a monitoring gap
MERGE_SECONDS = 300     # matching rows closer than this belong to one incident
LOSS_INCIDENT_PCT = 30  # per-target loss that counts as a packet-loss incident
MEMORY_DAYS = 90        # newest days kept fully in memory; older ones are summarised
PAST_GAP_DAYS = 365     # daily files before a gap this long in the history are ignored
AGG_BUCKET = 300        # resolution of the per-day summaries used for older days
AGG_VERSION = 1
COLD_CACHE_DAYS = 7     # older days parsed on demand and kept for reuse

DAILY_RE = re.compile(r"^NetMonitor_(\d{4}-\d{2}-\d{2})_([\w-]+)\.csv$")
LOG_RE = re.compile(
    r"^\s*(?P<target>\S+)\s*:\s*xmt/rcv/%loss\s*=\s*(?P<xmt>\d+)/(?P<rcv>\d+)/(?P<loss>\d+)%"
    r"(?:,\s*min/avg/max\s*=\s*(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+))?"
)
LISTING_RE = re.compile(
    r'<a href="(?P<href>[^"]+)">[^<]*</a></td><td class="m">(?P<mtime>[^<]*)</td>'
    r'<td class="s">(?P<size>[^<]*)</td>'
)


def daily_date(name):
    """'YYYY-MM-DD' for a NetMonitor daily CSV name, else None."""
    m = DAILY_RE.match(name)
    return m.group(1) if m else None


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return NAN


def target_kind(name):
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return "host"
    return "gateway" if ip.is_private else "ip"


def to_epoch(date_s, time_s, tz_s):
    dt = datetime.strptime(date_s + " " + time_s, "%Y-%m-%d %H:%M:%S")
    off = TZ_OFFSETS.get(tz_s.strip().upper())
    if off is None:  # unknown zone: assume the server shares the device's zone
        return time.mktime(dt.timetuple())
    return calendar.timegm(dt.timetuple()) - off * 3600


def jnum(v, nd=3):
    """JSON-safe float (NaN -> None)."""
    if v is None or v != v:
        return None
    return round(v, nd)


# ---------------------------------------------------------------- parsing ---

class Targets:
    """Global, ordered registry of every target name seen in any file."""

    def __init__(self):
        self.names = []
        self.index = {}

    def get(self, name):
        i = self.index.get(name)
        if i is None:
            i = self.index[name] = len(self.names)
            self.names.append(name)
        return i

    def info(self):
        return [{"name": n, "kind": target_kind(n)} for n in self.names]


TARGETS = Targets()


def row_ts(fields):
    """Timestamp of a CSV data row, or None for the header and malformed rows."""
    if len(fields) < 4 or fields[0] == "Date":
        return None
    try:
        return to_epoch(fields[0], fields[1], fields[2])
    except ValueError:
        return None


def parse_row(fields):
    """Return (ts, {target_idx: (xmt, rcv, loss, min, avg, max)}, dns_fail, markers) or None."""
    ts = row_ts(fields)
    if ts is None:
        return None
    blocks = {}
    dns_fail = False
    markers = []
    for i in range(4, len(fields), 7):
        name = fields[i].strip()
        if not name:
            continue
        if name.upper().startswith("DNS:"):
            dns_fail = True
            markers.append(name)
            continue
        vals = fields[i + 1:i + 7]
        vals += [""] * (6 - len(vals))
        blocks[TARGETS.get(name)] = tuple(fnum(v) for v in vals)
    return ts, blocks, dns_fail, markers


class Series:
    """Columnar storage of parsed rows from one CSV file (append-only)."""

    def __init__(self):
        self.ts = array.array("d")
        self.dns = array.array("b")
        self.markers = {}          # row index -> list of marker strings
        self.cols = {}             # target idx -> {"loss","min","avg","max"} arrays
        self.n = 0
        self.last_dns = None       # index of the most recent DNS-failure row
        self.ordered = True        # timestamps never decrease (bisect is valid)

    def _col(self, t):
        c = self.cols.get(t)
        if c is None:
            c = {k: array.array("d", [NAN] * self.n) for k in ("loss", "min", "avg", "max")}
            self.cols[t] = c
        return c

    def add(self, parsed):
        ts, blocks, dns_fail, markers = parsed
        for t in blocks:
            self._col(t)
        for t, c in self.cols.items():
            b = blocks.get(t)
            if b is None:
                for k in c:
                    c[k].append(NAN)
            else:
                c["loss"].append(b[2])
                c["min"].append(b[3])
                c["avg"].append(b[4])
                c["max"].append(b[5])
        if self.n and ts < self.ts[-1]:
            self.ordered = False
        self.ts.append(ts)
        self.dns.append(1 if dns_fail else 0)
        if markers:
            self.markers[self.n] = markers
        if dns_fail:
            self.last_dns = self.n
        self.n += 1

    def feed(self, text, after=None):
        """Parse CSV text, skipping rows not newer than `after` (rows already
        taken from the archive)."""
        for fields in csv.reader(io.StringIO(text)):
            p = parse_row(fields)
            if p and (after is None or p[0] > after):
                self.add(p)


def first_row_ts(data):
    """Timestamp of the first data row in raw CSV bytes, or None.  Runs without
    data_lock, so it must not register targets the way parse_row does."""
    for line in data[:65536].decode("utf-8", "replace").splitlines():
        ts = row_ts(next(csv.reader([line]), []))
        if ts is not None:
            return ts
    return None


# ---------------------------------------------------------------- fetching --

class RemoteFile:
    """A device file mirrored locally; refreshed incrementally with HTTP Range.

    lighttpd changes a file's ETag on every append, so If-Range can't be used to
    detect rewrites.  Instead each Range request re-reads the last OVERLAP cached
    bytes; if they no longer match, the device rewrote the file and it is fetched
    whole.  Only the size, those tail bytes and any unparsed partial line are kept
    in memory; new bytes are appended to the disk cache.  Mutation happens under
    store.data_lock.

    When the device loses power it deletes all its files and starts a fresh one.
    A replacement whose first row is newer than everything we hold is therefore a
    restart, not a rewrite: the rows we already have move to <name>.archive and
    the new file's rows are appended after them, so no history is lost.
    """

    OVERLAP = 256

    def __init__(self, store, name, load=True):
        self.store = store
        self.name = name
        self.pinned = False        # being refreshed; the cold-day cache must not unload it
        self.size = 0              # bytes of the device file we hold
        self.tail = b""            # last OVERLAP of those bytes, for rewrite detection
        self.pending = b""         # trailing partial line not yet parsed
        self.mtime = None          # listing mtime string the data corresponds to
        self.gen = 0               # bumped whenever the series is rebuilt from scratch
        self.archived_upto = None  # last timestamp moved to the archive by a device restart
        self.series = Series() if load else None
        if load:
            self._load_disk()
        else:
            self._read_meta()

    def _read_meta(self):
        try:
            with open(self.cache_path + ".meta") as f:
                self.mtime = json.load(f).get("mtime")
        except (OSError, ValueError):
            pass

    def load(self):
        """Parse the disk cache into memory (no-op when already loaded)."""
        if self.series is None:
            self.size, self.tail, self.pending = 0, b"", b""
            self.series = Series()
            self._load_disk()
        return self.series

    def unload(self):
        self.series = None
        self.size, self.tail, self.pending = 0, b"", b""

    @property
    def cache_path(self):
        return os.path.join(self.store.cache_dir, self.name)

    @property
    def archive_path(self):
        return self.cache_path + ".archive"

    def _load_archive(self):
        """Parse archived pre-restart rows; return the last archived timestamp."""
        try:
            with open(self.archive_path, "rb") as f:
                self.series.feed(f.read().decode("utf-8", "replace"))
        except OSError:
            pass
        return self.series.ts[-1] if self.series.n else None

    def _load_disk(self):
        self.archived_upto = self._load_archive()
        try:
            with open(self.cache_path + ".meta") as f:
                meta = json.load(f)
            with open(self.cache_path, "rb") as f:
                data = f.read()
        except (OSError, ValueError):
            return
        self.mtime = meta.get("mtime")
        self._ingest(data, self.archived_upto)

    def _archive_cache(self):
        """Move the complete lines of the current cache file onto the archive."""
        try:
            with open(self.cache_path, "rb") as f:
                data = f.read()
        except OSError:
            return
        data = data[:data.rfind(b"\n") + 1]
        try:
            with open(self.archive_path, "ab") as f:
                f.write(data)
        except OSError as e:
            print("archive write failed:", e, file=sys.stderr)

    def _save_disk(self, data, append):
        try:
            os.makedirs(self.store.cache_dir, exist_ok=True)
            with open(self.cache_path, "ab" if append else "wb") as f:
                f.write(data)
            with open(self.cache_path + ".meta", "w") as f:
                json.dump({"mtime": self.mtime}, f)
        except OSError as e:
            print("cache write failed:", e, file=sys.stderr)

    def _ingest(self, data, after=None):
        """Account for bytes appended to the file and parse the complete lines."""
        self.size += len(data)
        self.tail = (self.tail + data)[-self.OVERLAP:]
        buf = self.pending + data
        end = buf.rfind(b"\n") + 1  # never parse a half-written last line
        if end:
            self.series.feed(buf[:end].decode("utf-8", "replace"), after)
        self.pending = buf[end:]

    def _get(self, first_byte=None):
        url = urllib.parse.urljoin(self.store.device, urllib.parse.quote(self.name))
        req = urllib.request.Request(url)
        if first_byte is not None:
            req.add_header("Range", "bytes=%d-" % first_byte)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()

    def _fetch_new(self, mtime_changed):
        """Return (appended_bytes, None) or (None, full_body)."""
        if self.size:
            k = len(self.tail)
            start = self.size - k
            try:
                status, body = self._get(start)
            except urllib.error.HTTPError as e:
                if e.code != 416:   # 416: file is now shorter than our copy
                    raise
                status, body = None, b""
            # a changed mtime with nothing appended means an in-place rewrite of
            # the same length; this also fires (harmlessly) when a write lands
            # between reading the listing and fetching
            if status == 206 and body[:k] == self.tail and (len(body) > k or not mtime_changed):
                return body[k:], None
            if status == 200:
                return None, body
        return None, self._get()[1]

    def refresh(self, listing_mtime):
        """Fetch new data (network, no lock held), then apply it under the data lock."""
        # the listing mtime has 1s resolution, so a copy that ends mid-line may
        # have been read during a write in that same second: fetch again
        mtime_changed = listing_mtime != self.mtime
        if listing_mtime and not mtime_changed and self.size and not self.pending:
            return
        tail, full = self._fetch_new(mtime_changed)
        restart = False
        if full is not None and self.series.n:
            last = self.series.ts[-1]
            first = first_row_ts(full)
            # a file with no rows yet (header only, right after power returns) is a
            # restart too; rows already archived need not be archived again
            if self.archived_upto is None or last > self.archived_upto:  # rows not yet archived
                ours = [t for t in self.series.ts if self.archived_upto is None or t > self.archived_upto]
                # starting before our copy does means the device clock went back on
                # restart; a genuine rewrite keeps the same first row
                restart = first is None or first > last or first < ours[0]
            if restart:  # device lost power and started over: keep what we have
                print("%s restarted on the device; archiving %d earlier rows"
                      % (self.name, len(ours)), file=sys.stderr, flush=True)
                self._archive_cache()
                self.archived_upto = self.series.ts[-1]
        with self.store.data_lock:
            if full is not None:
                self.size, self.tail, self.pending = 0, b"", b""
                if not restart:  # genuine rewrite: rebuild from the archive plus the new body
                    self.series = Series()
                    self.gen += 1
                    self.archived_upto = self._load_archive()
            self._ingest(tail if full is None else full, self.archived_upto)
            self.mtime = listing_mtime
        if full is not None:
            self._save_disk(full, append=False)
        elif tail or mtime_changed:
            self._save_disk(tail, append=True)


class Store:
    LOG_NAME = "Latest_NetMonitor_Results.log"

    def __init__(self, device, cache_dir, memory_days=MEMORY_DAYS):
        self.device = device if device.endswith("/") else device + "/"
        self.cache_dir = cache_dir
        self.memory_days = memory_days
        self.cutoff = ""           # daily files dated before this are "cold" (summarised)
        self.cold_cache = OrderedDict()  # name -> RemoteFile of cold days parsed on demand
        self.aggs = {}             # name -> compacted per-day summary of a cold day
        self.files = {}            # name -> RemoteFile
        self.listing = []          # [{name, mtime, size}]
        self.last_error = None     # listing (device reachability) error
        self.file_errors = {}      # name -> error from the last attempt
        self.last_ok = None
        self.log = {"lines": []}   # parsed latest-results log
        self.log_mtime = None
        self.log_complete = False
        self.data_lock = threading.RLock()   # guards files/series for readers and writers
        self.sync_lock = threading.Lock()
        self._wake = threading.Event()
        self.summary_cache = {}
        self.floor = ""            # daily files dated before this are ignored (reset device clock)
        self.warned_dates = set()  # implausibly dated files already logged
        self.synced = False        # first sync attempt finished (cached data may be old until then)

    # -- background sync: requests never wait on the device
    def start(self):
        threading.Thread(target=self._loop, name="device-sync", daemon=True).start()

    def wake(self):
        self._wake.set()

    def _loop(self):
        while True:
            try:
                self.refresh()
                self.retire_cold()  # also when the device is unreachable
            except Exception:  # never let one bad cycle end syncing for good
                import traceback
                traceback.print_exc()
            self._wake.wait(INDEX_TTL)
            self._wake.clear()

    def refresh(self):
        with self.sync_lock:
            try:
                with urllib.request.urlopen(self.device, timeout=10) as r:
                    html = r.read().decode("utf-8", "replace")
            except Exception as e:  # device unreachable: keep serving cached data
                self.last_error = "%s: %s" % (type(e).__name__, e)
                self.synced = True
                return
            listing = []
            for m in LISTING_RE.finditer(html):
                name = urllib.parse.unquote(m.group("href"))
                if "/" not in name:
                    listing.append({"name": name, "mtime": m.group("mtime"), "size": m.group("size")})
            self.listing = listing
            self.last_ok = time.time()
            self.last_error = None
            for item in listing:
                name = item["name"]
                try:
                    if name.endswith(".csv"):
                        f = self.files.get(name)
                        if f is None:
                            with self.data_lock:  # loading the disk cache registers targets
                                f = self.files[name] = RemoteFile(self, name)
                                self._update_cutoff()
                        if f.series is None and item["mtime"] == f.mtime:  # cold day not in memory
                            self.file_errors.pop(name, None)
                            continue
                        with self.data_lock:  # a cold-cache eviction must not unload it mid-refresh
                            f.pinned = True
                            f.load()
                        try:
                            f.refresh(item["mtime"])
                        finally:
                            f.pinned = False
                    elif name == self.LOG_NAME and (item["mtime"] != self.log_mtime or not self.log_complete):
                        text = self.fetch_text(name)
                        self.log = parse_log(text)
                        self.log_mtime = item["mtime"]
                        self.log_complete = text.endswith("\n")
                    self.file_errors.pop(name, None)
                except Exception as e:  # one bad file must not stall the rest
                    self.file_errors[name] = "%s: %s" % (type(e).__name__, e)
            self.synced = True

    def load_cache(self):
        try:
            names = [n for n in os.listdir(self.cache_dir) if n.endswith(".csv")]
        except OSError:
            return
        self._update_cutoff(names)
        for n in names:
            if n not in self.files:
                d = daily_date(n)
                self.files[n] = RemoteFile(self, n, load=not (d and d < self.cutoff))

    # -- in-memory window: the newest memory_days days are parsed and kept;
    # older ("cold") days are served from per-day summaries on disk, and parsed
    # into a small LRU cache only when a short range asks for their rows

    @staticmethod
    def date_limit():
        """Latest plausible file date.  A device that lost power may log under a
        wrong (future) date until its clock is set; such files are ignored."""
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    def ignored_files(self):
        """Daily files ignored for an implausible date (see _update_cutoff)."""
        limit = self.date_limit()
        return sorted(n for n in self.files if (d := daily_date(n)) and not self.floor <= d <= limit)

    def _update_cutoff(self, names=None):
        """Recompute the plausible date range and the in-memory window.  Files dated
        ahead of the host's clock, or before a gap of over PAST_GAP_DAYS in the
        history (a device clock reset to its epoch, e.g. 2000-01-01), are ignored."""
        names = list(names or self.files)
        limit = self.date_limit()
        dates = sorted({d for d in map(daily_date, names) if d and d <= limit})
        self.floor = next((dates[k] for k in range(len(dates) - 1, 0, -1)
                           if (date.fromisoformat(dates[k]) - date.fromisoformat(dates[k - 1])).days
                           > PAST_GAP_DAYS), "")
        for n in names:
            d = daily_date(n)
            if d and not self.floor <= d <= limit and n not in self.warned_dates:
                self.warned_dates.add(n)
                print("ignoring %s: dated %s (device clock wrong?)"
                      % (n, "in the future" if d > limit else "long before the rest of the history"),
                      file=sys.stderr, flush=True)
        if dates:
            newest = datetime.strptime(dates[-1], "%Y-%m-%d")
            self.cutoff = (newest - timedelta(days=self.memory_days - 1)).strftime("%Y-%m-%d")

    def is_cold(self, f):
        d = daily_date(f.name)
        return d is not None and d < self.cutoff

    def ensure_loaded(self, f):
        """Rows of a cold day, parsed on demand.  Caller holds data_lock."""
        f.load()
        self.cold_cache[f.name] = f
        self.cold_cache.move_to_end(f.name)
        while len(self.cold_cache) > COLD_CACHE_DAYS:
            victim = next((g for g in self.cold_cache.values() if not g.pinned), None)
            if victim is None or victim is f:
                break
            del self.cold_cache[victim.name]
            victim.unload()
            self.summary_cache.pop(victim.name, None)
        return f.series

    def _agg_path(self, f):
        return f.cache_path + ".agg.json"

    def agg(self, f, build=True, prev_last=None):
        """Summary of a cold day: memory, then disk, then built from its rows.
        prev_last (the previous day's last sample) is looked up when not given."""
        a = self.aggs.get(f.name)
        if a is not None and a["mtime"] == f.mtime:
            return None if a.get("empty") else a
        try:
            with open(self._agg_path(f)) as fh:
                a = json.load(fh)
            if a.get("version") == AGG_VERSION and a.get("mtime") == f.mtime:
                self.aggs[f.name] = compact_agg(a)
                return a
        except (OSError, ValueError):
            pass
        if not build:
            return None
        s = self.ensure_loaded(f)
        if not s.n:  # remember it, or every request would parse the file again
            self.aggs[f.name] = {"mtime": f.mtime, "empty": True}
            return None
        a = build_agg(daily_date(f.name), f, s, prev_last if prev_last is not None else self._prev_last(f))
        try:
            with open(self._agg_path(f), "w") as fh:
                json.dump(a, fh, separators=(",", ":"))
        except OSError as e:
            print("summary write failed:", e, file=sys.stderr)
        self.aggs[f.name] = compact_agg(a)
        return a

    def _prev_last(self, f):
        """Last sample of the day before f (for the midnight carry-over)."""
        prev = None
        for _d, g in self.daily():
            if g is f:
                break
            prev = g
        if prev is None:
            return None
        if self.is_cold(prev):
            a = self.agg(prev, build=False)  # built oldest-first, so normally present
            return a["last"] if a else None
        s = prev.series
        return s.ts[-1] if s is not None and s.n else None

    def retire_cold(self):
        """Make sure every cold day has a summary and drop its rows from memory.
        Runs on the sync thread, oldest day first, one day per lock hold."""
        with self.data_lock:
            self._update_cutoff()  # date_limit() moves with the clock, not only with new files
            days = [f for _d, f in self.daily()]
        prev_last = None
        for f in days:
            with self.data_lock:
                if not self.is_cold(f):
                    s = f.series
                    prev_last = s.ts[-1] if s is not None and s.n else prev_last
                    continue
                a = self.agg(f, prev_last=prev_last)
                prev_last = a["last"] if a else prev_last
                if f.series is not None and f.name not in self.cold_cache and not f.pinned:
                    f.unload()
                    self.summary_cache.pop(f.name, None)

    def daily(self):
        out = []
        limit = self.date_limit()
        for name, f in self.files.items():
            d = daily_date(name)
            if d is not None and self.floor <= d <= limit:
                out.append((d, f))
        out.sort(key=lambda x: x[0])
        return out

    def events(self):
        for name, f in self.files.items():
            if "event" in name.lower():
                return f
        return None

    def fetch_text(self, name):
        url = urllib.parse.urljoin(self.device, urllib.parse.quote(name))
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- analysis --

def iter_rows(store, start, end, load_cold=False):
    """Yield (series, i) for rows with start <= ts < end, chronologically.

    Days older than the in-memory window are skipped unless load_cold, in which
    case they are parsed into the small cold-day cache."""
    for _date, f in store.daily():
        if store.is_cold(f):
            if not load_cold:
                continue
            a = store.agg(f)
            if not a or a["last"] < start or a["first"] >= end:
                continue
            s = store.ensure_loaded(f)
        else:
            s = f.series
        if s is None or not s.n:
            continue
        for i in row_range(s, start, end):
            yield s, i


def row_range(s, start, end):
    """Indices of rows with start <= ts < end.  Bisects when the series is in
    time order; a device clock stepped back mid-file needs a full scan."""
    if s.ordered:
        return range(bisect.bisect_left(s.ts, start), bisect.bisect_left(s.ts, end))
    return sorted((i for i in range(s.n) if start <= s.ts[i] < end), key=s.ts.__getitem__)


def classify_targets():
    kinds = {i: target_kind(n) for i, n in enumerate(TARGETS.names)}
    gw = [i for i, k in kinds.items() if k == "gateway"]
    ext = [i for i, k in kinds.items() if k == "ip"]
    hosts = [i for i, k in kinds.items() if k == "host"]
    return gw, ext, hosts


def row_state(s, i, gw, ext, hosts):
    cache = s.__dict__.setdefault("_state", {})
    key = len(TARGETS.names)
    if cache.get("key") != key:
        cache.clear()
        cache["key"] = key
        cache["rows"] = []
    rows = cache["rows"]
    while len(rows) <= i:
        rows.append(None)
    if rows[i] is None:
        rows[i] = _row_state(s, i, gw, ext, hosts)
    return rows[i]


def _row_state(s, i, gw, ext, hosts):
    def loss(t):
        c = s.cols.get(t)
        return c["loss"][i] if c else NAN

    lan_down = bool(gw) and all(loss(t) >= 100 for t in gw)
    ext_l = [loss(t) for t in ext if loss(t) == loss(t)]
    wan_down = (not lan_down) and bool(ext_l) and all(v >= 100 for v in ext_l)
    dns = bool(s.dns[i])
    missing = [TARGETS.names[t] for t in hosts if not (loss(t) == loss(t))]
    worst = 0.0
    worst_t = None
    for t in s.cols:
        v = loss(t)
        if v == v and v > worst:
            worst, worst_t = v, t
    return {"lan_down": lan_down, "wan_down": wan_down, "dns": dns,
            "missing": missing, "worst_loss": worst, "worst_target": worst_t}


def series_payload(store, start, end, points=600):
    span = max(end - start, 1)
    nice = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600]
    bucket = next((b for b in nice if span / b <= points), max(43200, math.ceil(span / points)))
    # days outside the in-memory window come from their 5-minute summaries when
    # the requested resolution allows it, else they are loaded on demand
    cold = []
    for _d, f in store.daily():
        a = store.agg(f) if store.is_cold(f) else None
        if a and a["last"] >= start and a["first"] < end:
            cold.append(a)
    rows = list(iter_rows(store, start, end))
    est = len(rows) + sum(n for a in cold for tb, n in zip(a["t"], a["samples"])
                          if start <= tb + AGG_BUCKET / 2 < end)
    use_aggs = bool(cold) and bucket >= AGG_BUCKET and est > points
    if cold and not use_aggs:
        rows = list(iter_rows(store, start, end, load_cold=True))
    if not rows and not use_aggs:
        return {"start": start, "end": end, "bucket": 0, "t": [], "targets": {}, "dns": [], "samples": []}
    if not use_aggs and len(rows) <= points:  # raw resolution when it fits
        bucket = 0
    ntargets = len(TARGETS.names)
    if bucket == 0:
        t = [s.ts[i] for s, i in rows]
        out = {n: {"avg": [], "min": [], "max": [], "loss": []} for n in TARGETS.names}
        for s, i in rows:
            for ti in range(ntargets):
                c = s.cols.get(ti)
                o = out[TARGETS.names[ti]]
                for k in ("avg", "min", "max", "loss"):
                    o[k].append(jnum(c[k][i]) if c else None)
        return {"start": start, "end": end, "bucket": 0, "t": t, "targets": out,
                "dns": [int(s.dns[i]) for s, i in rows], "samples": [1] * len(rows)}

    nb = int(math.ceil(span / bucket))
    acc = [[[0.0, 0, math.inf, -math.inf, 0.0, 0] for _ in range(nb)] for _ in range(ntargets)]
    dns = [0] * nb
    samples = [0] * nb
    for s, i in rows:
        b = min(int((s.ts[i] - start) // bucket), nb - 1)
        samples[b] += 1
        dns[b] += s.dns[i]
        for ti, c in s.cols.items():
            a = acc[ti][b]
            v = c["avg"][i]
            if v == v:
                a[0] += v
                a[1] += 1
            v = c["min"][i]
            if v == v and v < a[2]:
                a[2] = v
            v = c["max"][i]
            if v == v and v > a[3]:
                a[3] = v
            v = c["loss"][i]
            if v == v:
                a[4] += v
                a[5] += 1
    if use_aggs:
        for ag in cold:
            for j, tb in enumerate(ag["t"]):
                mid = tb + AGG_BUCKET / 2
                if not start <= mid < end:
                    continue
                b = min(int((mid - start) // bucket), nb - 1)
                samples[b] += int(ag["samples"][j])
                dns[b] += int(ag["dns"][j])
                for name, o in ag["targets"].items():
                    a = acc[TARGETS.index[name]][b]
                    a[0] += o["avg_sum"][j]
                    a[1] += int(o["avg_n"][j])
                    v = o["min"][j]
                    if v == v and v < a[2]:
                        a[2] = v
                    v = o["max"][j]
                    if v == v and v > a[3]:
                        a[3] = v
                    a[4] += o["loss_sum"][j]
                    a[5] += int(o["loss_n"][j])
    t = [start + (b + 0.5) * bucket for b in range(nb)]
    out = {}
    for ti, name in enumerate(TARGETS.names):
        o = {"avg": [], "min": [], "max": [], "loss": []}
        for a in acc[ti]:
            o["avg"].append(jnum(a[0] / a[1]) if a[1] else None)
            o["min"].append(jnum(a[2]) if a[2] != math.inf else None)
            o["max"].append(jnum(a[3]) if a[3] != -math.inf else None)
            o["loss"].append(jnum(a[4] / a[5], 2) if a[5] else None)
        out[name] = o
    return {"start": start, "end": end, "bucket": bucket, "t": t, "targets": out,
            "dns": dns, "samples": samples}


def gap_incident(a, b):
    return {"kind": "gap", "start": a, "end": b, "rows": 0,
            "detail": "No samples recorded for %d min (device offline or lost power)" % round((b - a) / 60)}


def scan_fragments(rows):
    """Group one day's consecutive problem rows into incident fragments.

    Fragments are merged across days by incidents(); a day's summary stores its
    fragments so days outside the in-memory window need no row data."""
    gw, ext, hosts = classify_targets()
    open_ = {}   # kind -> fragment
    frags, gaps = [], []
    first = prev_ts = None
    for s, i in rows:
        ts = s.ts[i]
        if first is None:
            first = ts
        if prev_ts is not None and ts - prev_ts > GAP_SECONDS:
            gaps.append(gap_incident(prev_ts, ts))
        before = prev_ts if prev_ts is not None and ts - prev_ts <= GAP_SECONDS else ts
        prev_ts = ts
        st = row_state(s, i, gw, ext, hosts)
        lossy = st["worst_loss"] >= LOSS_INCIDENT_PCT and not (st["lan_down"] or st["wan_down"])
        flags = {"lan_down": st["lan_down"], "wan_down": st["wan_down"], "dns": st["dns"], "loss": lossy}
        for kind, on in flags.items():
            inc = open_.get(kind)
            if inc and (not on or ts - inc["last"] > MERGE_SECONDS):
                frags.append(open_.pop(kind))
                inc = None
            if not on:
                continue
            if inc is None:
                # rows are stamped when a probe cycle finishes, so the fault began
                # somewhere after the previous sample
                inc = open_[kind] = {"kind": kind, "start": before, "first_row": ts, "last": ts, "rows": 0,
                                     "hosts": {}, "wan_rows": 0, "lan_rows": 0, "targets": {}, "peak": 0}
            inc["last"] = ts
            inc["rows"] += 1
            if kind == "dns":
                for h in st["missing"]:
                    inc["hosts"][h] = inc["hosts"].get(h, 0) + 1
                if st["wan_down"]:
                    inc["wan_rows"] += 1
                if st["lan_down"]:
                    inc["lan_rows"] += 1
            if kind == "loss":
                for ti, c in s.cols.items():
                    v = c["loss"][i]
                    if v == v and v >= LOSS_INCIDENT_PCT:
                        n = TARGETS.names[ti]
                        inc["targets"][n] = max(inc["targets"].get(n, 0), v)
                inc["peak"] = max(inc["peak"], st["worst_loss"])
    frags.extend(open_.values())
    frags.sort(key=lambda x: x["first_row"])
    return {"first": first, "last": prev_ts, "frags": frags, "gaps": gaps}


def merge_fragment(a, b):
    a["last"] = b["last"]
    a["rows"] += b["rows"]
    for h, n in b["hosts"].items():
        a["hosts"][h] = a["hosts"].get(h, 0) + n
    a["wan_rows"] += b["wan_rows"]
    a["lan_rows"] += b["lan_rows"]
    for n, v in b["targets"].items():
        a["targets"][n] = max(a["targets"].get(n, 0), v)
    a["peak"] = max(a["peak"], b["peak"])


def incidents(store, start, end):
    """Problem periods in [start, end): per-day fragments merged across midnight."""
    segs = []
    for _date, f in store.daily():
        if store.is_cold(f):
            a = store.agg(f)
            if not a or a["last"] < start or a["first"] >= end:
                continue
            segs.append({"first": a["first"], "last": a["last"],
                         "frags": [copy.deepcopy(x) for x in a["frags"]
                                   if x["last"] >= start and x["start"] < end],
                         "gaps": [dict(g) for g in a["gaps"] if g["end"] > start and g["start"] < end]})
            continue
        s = f.series
        if s is None or not s.n:
            continue
        idx = row_range(s, start, end)
        if idx:
            segs.append(scan_fragments((s, i) for i in idx))

    done = []
    open_ = {}       # kind -> fragment that reached the end of the previous day
    prev_last = None
    for seg in segs:
        if prev_last is not None and seg["first"] - prev_last > GAP_SECONDS:
            done.append(gap_incident(prev_last, seg["first"]))
        done.extend(seg["gaps"])
        carry, open_ = open_, {}
        for fr in seg["frags"]:
            prev = carry.pop(fr["kind"], None)
            at_start = fr["first_row"] == seg["first"]
            if prev and at_start and fr["first_row"] - prev["last"] <= MERGE_SECONDS:
                merge_fragment(prev, fr)
                fr = prev
            else:
                if prev:
                    done.append(prev)
                if at_start and prev_last is not None and seg["first"] - prev_last <= GAP_SECONDS:
                    fr["start"] = prev_last  # the fault began after the previous day's last sample
            if fr["last"] == seg["last"]:
                open_[fr["kind"]] = fr
            else:
                done.append(fr)
        done.extend(carry.values())
        prev_last = seg["last"]
    for inc in open_.values():
        inc["ongoing"] = (time.time() - inc["last"]) < MERGE_SECONDS
        done.append(inc)

    for inc in done:
        if "end" not in inc:
            inc["end"] = inc["last"]
        inc["duration"] = max(inc["end"] - inc["start"], 0)
        k = inc["kind"]
        if k == "dns":
            hosts_s = ", ".join(sorted(inc["hosts"])) or "hostname targets"
            if inc["lan_rows"] * 2 >= inc["rows"]:
                inc["cause"] = "lan"
                inc["detail"] = "Lookups failed for %s while the gateway was unreachable" % hosts_s
            elif inc["wan_rows"] * 2 >= inc["rows"]:
                inc["cause"] = "wan"
                inc["detail"] = "Lookups failed for %s while the internet link was down" % hosts_s
            else:
                inc["cause"] = "dns"
                inc["detail"] = "Lookups failed for %s while public IPs were reachable (DNS-only fault)" % hosts_s
            inc["hosts"] = sorted(inc["hosts"])
        elif k == "wan_down":
            inc["detail"] = "All public IP targets at 100% loss; gateway reachable"
        elif k == "lan_down":
            inc["detail"] = "Gateway unreachable (100% loss)"
        elif k == "loss":
            inc["detail"] = "Peak %d%% loss on %s" % (inc["peak"], ", ".join(
                "%s (%d%%)" % (n, v) for n, v in sorted(inc["targets"].items(), key=lambda x: -x[1])))
        for key in ("last", "first_row", "wan_rows", "lan_rows", "targets", "peak"):
            inc.pop(key, None)
        if k != "dns":
            inc.pop("hosts", None)
    done.sort(key=lambda x: x["start"], reverse=True)
    return done


def daily_summary(store):
    days = []
    prev_last = None
    for date, f in store.daily():
        if store.is_cold(f):
            a = store.agg(f)
            if a:
                days.append(a["summary"])
                prev_last = a["last"]
            continue
        s = f.series
        if s is None or not s.n:
            continue
        # past days never change: reuse the result while the inputs are identical
        key = (f.gen, s.n, len(TARGETS.names), prev_last)
        hit = store.summary_cache.get(f.name)
        if hit is None or hit[0] != key:
            hit = store.summary_cache[f.name] = (key, summarize_day(date, s, prev_last))
        days.append(hit[1])
        prev_last = s.ts[-1]
    return days


def summarize_day(date, s, prev_last):
    gw, ext, hosts = classify_targets()
    stats = {}
    for ti, c in s.cols.items():
        avg = [v for v in c["avg"] if v == v]
        loss = [v for v in c["loss"] if v == v]
        stats[TARGETS.names[ti]] = {
            "avg": jnum(sum(avg) / len(avg)) if avg else None,
            "p95": jnum(sorted(avg)[int(len(avg) * 0.95)]) if avg else None,
            "max": jnum(max((v for v in c["max"] if v == v), default=NAN)),
            "loss": jnum(sum(loss) / len(loss), 3) if loss else None,
        }
    down = {"lan_down": 0.0, "wan_down": 0.0, "dns": 0.0}
    dns_rows = 0
    covered = 0.0
    for i in range(s.n):
        # time covered by this cycle; the first row continues the previous day
        prev = s.ts[i - 1] if i else prev_last
        dt = s.ts[i] - prev if prev is not None and s.ts[i] > prev else 9.0
        covered += min(dt, GAP_SECONDS)
        st = row_state(s, i, gw, ext, hosts)
        for k in down:
            if st[k]:
                down[k] += min(dt, GAP_SECONDS)
        dns_rows += s.dns[i]
    span = s.ts[-1] - s.ts[0] + 9.0
    return {
        "date": date, "samples": s.n, "first": s.ts[0], "last": s.ts[-1],
        "coverage": jnum(min(covered / 86400.0, 1.0), 4),
        "span_coverage": jnum(min(covered / span, 1.0), 4),
        "dns_rows": dns_rows,
        "dns_seconds": round(down["dns"]),
        "wan_down_seconds": round(down["wan_down"]),
        "lan_down_seconds": round(down["lan_down"]),
        "targets": stats,
    }


AGG_KEYS = ("avg_sum", "avg_n", "min", "max", "loss_sum", "loss_n")


def build_agg(date, f, s, prev_last):
    """Compact, JSON-able digest of one day: summary row, 5-minute buckets and
    incident fragments.  Lets days outside the in-memory window be served
    without their rows."""
    t, samples, dns = [], [], []
    tg = {TARGETS.names[ti]: {k: [] for k in AGG_KEYS} for ti in s.cols}
    pos = -1
    for i in range(s.n):
        tb = s.ts[i] // AGG_BUCKET * AGG_BUCKET
        if not t or t[-1] != tb:
            t.append(tb)
            samples.append(0)
            dns.append(0)
            for o in tg.values():
                for k, v in zip(AGG_KEYS, (0.0, 0, None, None, 0.0, 0)):
                    o[k].append(v)
            pos += 1
        samples[pos] += 1
        dns[pos] += s.dns[i]
        for ti, c in s.cols.items():
            o = tg[TARGETS.names[ti]]
            v = c["avg"][i]
            if v == v:
                o["avg_sum"][pos] += v
                o["avg_n"][pos] += 1
            v = c["min"][i]
            if v == v and (o["min"][pos] is None or v < o["min"][pos]):
                o["min"][pos] = v
            v = c["max"][i]
            if v == v and (o["max"][pos] is None or v > o["max"][pos]):
                o["max"][pos] = v
            v = c["loss"][i]
            if v == v:
                o["loss_sum"][pos] += v
                o["loss_n"][pos] += 1
    frag = scan_fragments((s, i) for i in range(s.n))
    last_dns = None
    if s.last_dns is not None:
        last_dns = {"ts": s.ts[s.last_dns], "markers": s.markers.get(s.last_dns, [])}
    return {"version": AGG_VERSION, "mtime": f.mtime, "date": date, "first": s.ts[0], "last": s.ts[-1],
            "summary": summarize_day(date, s, prev_last), "t": t, "samples": samples, "dns": dns,
            "targets": tg, "frags": frag["frags"], "gaps": frag["gaps"], "last_dns": last_dns}


def compact_agg(a):
    """Swap an agg's per-bucket lists for float arrays (about 4x less memory)."""
    def arr(xs):
        return array.array("d", (NAN if v is None else v for v in xs))
    for k in ("t", "samples", "dns"):
        a[k] = arr(a[k])
    for name, o in a["targets"].items():
        TARGETS.get(name)
        for k in AGG_KEYS:
            o[k] = arr(o[k])
    return a


def parse_log(text):
    lines = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = LOG_RE.match(raw)
        if m:
            d = m.groupdict()
            lines.append({"target": d["target"], "xmt": int(d["xmt"]), "rcv": int(d["rcv"]),
                          "loss": int(d["loss"]), "min": fnum(d["min"]) if d["min"] else None,
                          "avg": fnum(d["avg"]) if d["avg"] else None,
                          "max": fnum(d["max"]) if d["max"] else None, "raw": raw})
        else:
            lines.append({"raw": raw})   # e.g. resolver errors - surface verbatim
    return {"lines": lines}


def status(store):
    gw, ext, hosts = classify_targets()
    daily = store.daily()
    # latest sample by time, not by file date; prefer one that isn't far in the
    # future (a mis-set device clock), but tolerate ordinary clock drift
    horizon = time.time() + 3600
    cands = [f.series for _d, f in daily
             if f.series is not None and f.series.n and not store.is_cold(f)]
    plausible = [s for s in cands if s.ts[-1] <= horizon] or cands
    last = max(plausible, key=lambda s: s.ts[-1], default=None)
    out = {"device": store.device, "device_error": store.last_error, "device_ok_at": store.last_ok,
           "now": time.time(), "targets": TARGETS.info(), "listing": store.listing}
    log = store.log
    out["log"] = log
    out["log_mtime"] = store.log_mtime
    out["syncing"] = not store.synced
    out["ignored_files"] = store.ignored_files()
    out["file_errors"] = dict(store.file_errors)  # sync thread mutates the original
    if last is None:
        out["last"] = None
        return out
    i = last.n - 1
    st = row_state(last, i, gw, ext, hosts)
    row = {}
    for ti, c in last.cols.items():
        row[TARGETS.names[ti]] = {k: jnum(c[k][i]) for k in c}
    log_hosts = {x.get("target") for x in log["lines"] if x.get("target")}
    log_missing = [n for n in (TARGETS.names[t] for t in hosts) if log["lines"] and n not in log_hosts]
    out["last"] = {"ts": last.ts[i], "targets": row, "dns_fail": bool(last.dns[i]),
                   "markers": last.markers.get(i, []), "missing": st["missing"],
                   "lan_down": st["lan_down"], "wan_down": st["wan_down"]}
    out["log_missing"] = log_missing
    # current DNS streak and last DNS failure seen anywhere
    # (a streak that began before midnight continues into the previous day's file)
    streak_start = None
    for s in reversed(cands[:next(k for k, c in enumerate(cands) if c is last) + 1]):
        j = s.n - 1
        while j >= 0 and s.dns[j] and (streak_start is None or streak_start - s.ts[j] <= GAP_SECONDS):
            streak_start = s.ts[j]
            j -= 1
        if j >= 0:
            break
    out["dns_streak_start"] = streak_start
    last_dns = None
    for _d, f in reversed(daily):
        if store.is_cold(f):
            a = store.agg(f)
            last_dns = a and a["last_dns"]
        elif f.series is not None and f.series.last_dns is not None:
            k = f.series.last_dns
            last_dns = {"ts": f.series.ts[k], "markers": f.series.markers.get(k, [])}
        if last_dns:
            break
    out["last_dns_failure"] = last_dns
    # sample interval, last 24h counters
    if last.n > 1:
        tail = [last.ts[k + 1] - last.ts[k] for k in range(max(0, last.n - 20), last.n - 1)]
        out["interval"] = sorted(tail)[len(tail) // 2]
    now = last.ts[i]
    c24 = {"samples": 0, "dns_rows": 0, "lossy_rows": 0, "wan_down_rows": 0}
    for s, k in iter_rows(store, now - 86400, now + 1):
        c24["samples"] += 1
        c24["dns_rows"] += s.dns[k]
        stk = row_state(s, k, gw, ext, hosts)
        c24["lossy_rows"] += 1 if stk["worst_loss"] > 0 else 0
        c24["wan_down_rows"] += 1 if stk["wan_down"] else 0
    out["last24"] = c24
    return out


def event_rows(store, start, end, offset, limit, only):
    f = store.events()
    if f is None:
        return {"total": 0, "rows": []}
    s = f.series
    gw, ext, hosts = classify_targets()
    idx = list(row_range(s, start, end))
    if only == "dns":
        idx = [i for i in idx if s.dns[i]]
    elif only == "loss":
        idx = [i for i in idx if any(c["loss"][i] > 0 for c in s.cols.values() if c["loss"][i] == c["loss"][i])]
    idx.reverse()
    total = len(idx)
    rows = []
    for i in idx[offset:offset + limit]:
        st = row_state(s, i, gw, ext, hosts)
        tg = {}
        for ti, c in s.cols.items():
            if c["loss"][i] == c["loss"][i]:
                tg[TARGETS.names[ti]] = {"loss": jnum(c["loss"][i]), "avg": jnum(c["avg"][i]),
                                          "max": jnum(c["max"][i])}
        rows.append({"ts": s.ts[i], "dns": bool(s.dns[i]), "missing": st["missing"],
                     "wan_down": st["wan_down"], "lan_down": st["lan_down"], "targets": tg})
    return {"total": total, "rows": rows}


# ---------------------------------------------------------------- http ------

class Handler(SimpleHTTPRequestHandler):
    store = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC_DIR, **kw)

    def log_message(self, fmt, *args):
        if os.environ.get("NETMON_VERBOSE"):
            super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            return super().do_GET()
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        st = self.store
        if q.get("force") == "1":
            st.wake()
        try:
            with st.data_lock:  # compute under the lock, write the socket outside it
                payload, code = self.api(u.path, q, st)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
        return self.send_json(payload, code)

    def api(self, path, q, st):
        if path == "/api/status":
            return clean(status(st)), 200
        if path == "/api/daily":
            return {"days": daily_summary(st), "targets": TARGETS.info()}, 200
        if path not in ("/api/series", "/api/incidents", "/api/events"):
            return {"error": "unknown endpoint"}, 404
        now = time.time()  # only the range endpoints take (and validate) parameters
        try:
            start = float(q.get("start", now - 86400))
            end = float(q.get("end", now + 60))
            points = min(max(int(q.get("points", 600)), 1), 5000)
            offset = max(int(q.get("offset", 0)), 0)
            limit = min(max(int(q.get("limit", 100)), 1), 1000)
        except ValueError as e:
            return {"error": "bad parameter: %s" % e}, 400
        if not (math.isfinite(start) and math.isfinite(end)):
            return {"error": "start and end must be finite"}, 400
        if path == "/api/series":
            return series_payload(st, start, end, points), 200
        if path == "/api/incidents":
            return {"incidents": incidents(st, start, end)}, 200
        return clean(event_rows(st, start, end, offset, limit, q.get("only", ""))), 200


def clean(o):
    """Recursively replace NaN with None so json.dumps(allow_nan=False) works."""
    if isinstance(o, float):
        return None if o != o else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def main():
    env = os.environ.get  # every option can also be set as NETMON_<NAME> (used by Docker)
    ap = argparse.ArgumentParser(description="NetMonitor dashboard server")
    ap.add_argument("--device", default=env("NETMON_DEVICE", "http://192.168.0.246/"))
    ap.add_argument("--host", default=env("NETMON_HOST", "127.0.0.1"),
                    help="bind address (0.0.0.0 to share on the LAN)")
    ap.add_argument("--port", type=int, default=int(env("NETMON_PORT", 8080)))
    ap.add_argument("--cache", default=env("NETMON_CACHE", os.path.join(HERE, "cache")))
    ap.add_argument("--memory-days", type=int, default=int(env("NETMON_MEMORY_DAYS", MEMORY_DAYS)),
                    help="newest days kept fully in memory; older days are served from summaries")
    args = ap.parse_args()
    # as PID 1 in a container SIGTERM is ignored unless handled; exit promptly on `docker stop`
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    store = Store(args.device, args.cache, max(1, args.memory_days))
    t0 = time.time()
    with store.data_lock:  # serve cached data right away; the device sync runs in the background
        store.load_cache()
    print("Loaded %d cached files in %.1fs; syncing from %s in the background"
          % (len(store.files), time.time() - t0, store.device), flush=True)
    store.start()
    Handler.store = store
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Dashboard: http://%s:%d/" % (args.host, args.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
