"""Resolve a search result to a torrent, whatever produced it.

Four kinds of thing arrive in `SearchResult.source_id`, and each is a
different amount of knowledge about the same object:

    magnet:?xt=urn:btih:...      a torrent, named and located
    3bf1...  (40 hex / 32 b32)   a torrent, named only
    https://archive.org/...      a .torrent file, located
    <archive.org identifier>     an item; its torrent has to be looked up

Only the last one needs the network, and it is the only host in the
manifest. The other three resolve offline, which is what lets this plugin
reach a torrent from a source it has never heard of without the manifest
having to name that source -- see `_from_info_hash`.

The host does the rest: it fetches the .torrent, computes the info-hash
from the bytes that actually arrived, checks it against whatever this
plugin claimed, and reads the file manifest. A plugin never opens a socket
and never runs a torrent client.
"""

import base64
import binascii
import json
import re
from posixpath import basename
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from rom_hub_sdk import SearchResult, TorrentProvider, TorrentSource

METADATA = "https://archive.org/metadata/"
DOWNLOAD = "https://archive.org/download/"

#: Archive.org names its torrent this way, and marks it with this `format`.
TORRENT_FORMAT = "Archive BitTorrent"

_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
_BASE32 = re.compile(r"[A-Z2-7]{32}")

#: Derivatives the Archive adds to every item. Excluded from the wanted-file
#: selection so a handoff does not name six files where one is the game.
_DERIVED_SUFFIXES = (
    "_meta.xml",
    "_files.xml",
    "_meta.sqlite",
    "_reviews.xml",
    "_archive.torrent",
    "__ia_thumb.jpg",
)


class TorrentRefused(Exception):
    """This item has no torrent, and the message says which kind of "no"."""


def _as_list(value) -> list[str]:
    """Archive.org returns `collection` as a list, or as a bare string when an
    item is in exactly one collection."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def normalise_info_hash(value: str) -> str | None:
    """A v1 info-hash as 40 lowercase hex, or None if this is not one.

    Both forms a magnet may carry are accepted. The base32 form is 32
    characters of RFC 4648 alphabet decoding to the same 20 bytes, and
    normalising it here means the host is handed one shape rather than two.
    """
    v = value.strip()
    if _HEX40.fullmatch(v):
        return v.lower()
    upper = v.upper()
    if _BASE32.fullmatch(upper):
        try:
            return base64.b32decode(upper).hex()
        except (binascii.Error, ValueError):
            return None
    return None


class Torrents(TorrentProvider):
    def resolve(self, result: SearchResult) -> TorrentSource:
        raw = (result.source_id or "").strip()
        if not raw:
            raise TorrentRefused("the search result carries no source id")

        title = (result.title or "").strip() or None
        scheme = urlsplit(raw).scheme.lower()

        if scheme == "magnet":
            return self._from_magnet(raw, title)

        direct = normalise_info_hash(raw)
        if direct:
            return self._from_info_hash(direct, title)

        if scheme in {"http", "https"}:
            return self._from_torrent_url(raw, title)

        return self._from_archive_item(raw, title)

    # -- a magnet somebody already has -------------------------------------

    def _from_magnet(self, uri: str, title: str | None) -> TorrentSource:
        """Reduce a magnet to what this plugin can honestly stand behind.

        A magnet is a bundle: an info-hash, which names bytes, plus trackers
        and web seeds, which name hosts. The host checks every one of those
        hosts against this plugin's allowlist -- correctly, because they are
        contacted -- and an indexer's magnet names trackers no static manifest
        could have declared. Passing them through unchanged would make every
        such magnet fail at the gate.

        So by default the info-hash is kept and the locations are dropped: a
        trackerless magnet, resolvable over DHT by the client the operator
        already runs. `keep_trackers` puts them back for an operator whose
        allowlist genuinely covers them.
        """
        split = urlsplit(uri)
        query = split.query or split.path.lstrip("?")
        pairs = parse_qsl(query, keep_blank_values=True)
        if not pairs:
            raise TorrentRefused(f"that magnet carries no parameters: {uri!r}")

        hashes, names, trackers, seeds = [], [], [], []
        for key, value in pairs:
            if key == "xt":
                urn = value.strip()
                if urn.lower().startswith("urn:btih:"):
                    got = normalise_info_hash(urn[len("urn:btih:"):])
                    if got:
                        hashes.append(got)
                elif urn.lower().startswith("urn:btmh:"):
                    raise TorrentRefused(
                        "that magnet names a v2 (BitTorrent v2) info-hash; this "
                        "host checks v1 info-hashes, so there is nothing it "
                        "could verify the download against"
                    )
            elif key == "dn":
                names.append(value.strip())
            elif key == "tr":
                trackers.append(value.strip())
            elif key == "ws":
                seeds.append(value.strip())

        if not hashes:
            raise TorrentRefused(
                f"that magnet has no usable v1 info-hash (`xt=urn:btih:`): {uri!r}"
            )
        if len(set(hashes)) > 1:
            raise TorrentRefused(
                "that magnet names more than one info-hash, so which torrent it "
                "means is a guess this plugin will not make"
            )

        display = title or (names[0] if names else None)
        keep = bool(self.ctx.config.get("keep_trackers"))
        if keep and (trackers or seeds):
            return TorrentSource(
                kind="magnet",
                source=self._magnet(hashes[0], display, trackers, seeds),
                name=display,
                info_hash=hashes[0],
                extra={"origin": "magnet", "trackers": str(len(trackers)),
                       "web_seeds": str(len(seeds))},
            )
        return TorrentSource(
            kind="magnet",
            source=self._magnet(hashes[0], display),
            name=display,
            info_hash=hashes[0],
            extra={
                "origin": "magnet",
                # Said plainly, because it changes how the torrent resolves:
                # without a tracker the client needs DHT to find peers.
                "trackers_dropped": str(len(trackers) + len(seeds)),
            },
        )

    # -- an info-hash from anywhere at all ---------------------------------

    def _from_info_hash(self, info_hash: str, title: str | None) -> TorrentSource:
        """The one form that reaches a source the manifest never named.

        An info-hash is a digest of the torrent's `info` dictionary. It names
        content, not a location -- there is no host in it to check, nothing to
        point at an internal address, and so nothing an allowlist has an
        opinion about. That is why this path works for a result from Prowlarr,
        from an indexer this plugin has never heard of, or from a list somebody
        pasted, without the manifest having to declare any of them and without
        the sandbox being loosened by one host.

        What it costs is the file manifest: with no .torrent to read, the host
        cannot list what is inside until the client resolves it.
        """
        return TorrentSource(
            kind="magnet",
            source=self._magnet(info_hash, title),
            name=title,
            info_hash=info_hash,
            extra={"origin": "info-hash"},
        )

    @staticmethod
    def _magnet(info_hash: str, name: str | None,
                trackers: list[str] | None = None,
                seeds: list[str] | None = None) -> str:
        params = [("xt", f"urn:btih:{info_hash}")]
        if name:
            params.append(("dn", name))
        for t in trackers or []:
            params.append(("tr", t))
        for w in seeds or []:
            params.append(("ws", w))
        return "magnet:?" + urlencode(params)

    # -- a .torrent URL ----------------------------------------------------

    def _from_torrent_url(self, url: str, title: str | None) -> TorrentSource:
        """Hand back a .torrent URL for the host to fetch and check.

        Not gated here: the host applies this plugin's allowlist to the URL and
        re-applies it on every redirect hop. Checking it a second time in the
        plugin would only produce a second, subtly different rule.
        """
        if not url.lower().endswith(".torrent"):
            raise TorrentRefused(
                f"that URL does not name a .torrent file: {url!r}. A link to a "
                f"download page is not a torrent; give the .torrent itself, the "
                f"magnet, or the info-hash"
            )
        return TorrentSource(
            kind="torrent_url",
            source=url,
            name=title,
            extra={"origin": "url"},
        )

    # -- an Archive.org item -----------------------------------------------

    def _from_archive_item(self, identifier: str, title: str | None) -> TorrentSource:
        """Look up the torrent the Internet Archive publishes for one item.

        The Archive makes and seeds a `<identifier>_archive.torrent` for most
        of what it holds, and lists it in the item's own metadata with a
        `btih` -- so the info-hash is known without fetching the torrent, and
        becomes a cross-check on the bytes the host later receives.

        Presence is *tested*, never inferred from an item's collections. An
        item in `stream_only` was expected to publish no torrent, and
        `arcadia_Soccer_1982_UA_EU-US_aka_Football` is in `stream_only` and
        publishes one. Reading the file list is the only thing that answers it.
        """
        item = self._metadata(identifier)

        if item.get("is_dark"):
            raise TorrentRefused(
                f"the Internet Archive has darkened {identifier!r}: it serves no "
                f"files and no torrent for it. This is a rights withdrawal at "
                f"the Archive's end, not a fetch that failed"
            )

        metadata = item.get("metadata")
        if not isinstance(metadata, dict) or not metadata:
            raise TorrentRefused(
                f"the Internet Archive has no item {identifier!r} (its metadata "
                f"endpoint returned nothing)"
            )

        files = item.get("files")
        files = files if isinstance(files, list) else []
        entry = next(
            (f for f in files
             if isinstance(f, dict) and f.get("format") == TORRENT_FORMAT),
            None,
        )
        if entry is None:
            raise TorrentRefused(
                f"the Internet Archive publishes no torrent for {identifier!r}. "
                f"Not every item has one -- an access-restricted item is the "
                f"usual reason -- and the item is still reachable over https"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise TorrentRefused(
                f"the torrent entry for {identifier!r} has no filename"
            )

        info_hash = normalise_info_hash(str(entry.get("btih") or ""))
        item_title = metadata.get("title")

        return TorrentSource(
            kind="torrent_url",
            source=DOWNLOAD + quote(identifier, safe="") + "/" + quote(name, safe=""),
            name=title or (item_title if isinstance(item_title, str) else None),
            files=self._wanted(files),
            info_hash=info_hash,
            extra={
                "origin": "archive.org",
                "identifier": identifier,
                # Both are the operator's cue for why an item that will not
                # import still has a torrent worth having.
                "stream_only": str(
                    "stream_only" in _as_list(metadata.get("collection"))
                ).lower(),
                "private": str(entry.get("private") or "false").lower(),
            },
        )

    def _wanted(self, files: list) -> list[str]:
        """Name the item's own files, as the torrent will carry them.

        Two things make this less obvious than filtering a list.

        `ia_make_torrent` **flattens** subdirectories: an item whose metadata
        lists `GameCube RvZ/Animal Crossing (USA, Canada).rvz` carries it in
        the torrent as the bare name. A selector is matched against the
        torrent's entries, so selecting by the metadata path would name
        something the torrent does not contain. The basename is what is there.

        And a selector must be a bare filename, so an entry that flattens onto
        another one is dropped rather than sent twice -- the host refuses a
        repeated selector, and one ambiguous name should not cost the operator
        every other file in the item.
        """
        if str(self.ctx.config.get("archive_files") or "payload") != "payload":
            return []

        seen: dict[str, int] = {}
        ordered: list[str] = []
        for f in files:
            if not isinstance(f, dict) or f.get("source") != "original":
                continue
            name = f.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name.endswith(_DERIVED_SUFFIXES):
                continue
            flat = basename(name)
            if not flat:
                continue
            key = flat.casefold()
            if key not in seen:
                ordered.append(flat)
            seen[key] = seen.get(key, 0) + 1

        return [n for n in ordered if seen[n.casefold()] == 1]

    def _metadata(self, identifier: str) -> dict:
        url = METADATA + quote(identifier, safe="")
        response = self.ctx.http.get(url)
        if response.status_code != 200:
            raise TorrentRefused(
                f"the Internet Archive returned HTTP {response.status_code} for "
                f"the metadata of {identifier!r}"
            )
        try:
            item = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise TorrentRefused(
                f"the Internet Archive's metadata for {identifier!r} was not "
                f"JSON: {exc}"
            ) from exc
        if not isinstance(item, dict):
            raise TorrentRefused(
                f"the Internet Archive's metadata for {identifier!r} was not an "
                f"object"
            )
        return item
