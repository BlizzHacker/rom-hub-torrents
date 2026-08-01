# Torrents — a ROM Hub plugin

Resolves a search result to a torrent, whatever produced it: an Internet Archive
item, a magnet, a bare info-hash, or a `.torrent` URL.

The plugin never opens a socket and never runs a torrent client. It returns a
location; the host fetches it, computes the info-hash from the bytes that arrived,
checks that against what the plugin claimed, and reads the file manifest.

[![licence MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
[![RPP v1](https://img.shields.io/badge/RPP-v1-informational)](https://github.com/BlizzHacker/rom-hub)

---

## Install

```bash
rom-hub catalog add torrents https://raw.githubusercontent.com/BlizzHacker/rom-hub/master/catalog/torrents.json
rom-hub plugin install torrents
```

Or from ROMarr: **Hub → Plugins → Install**.

## Usage

```bash
rom-hub torrent show    torrents <source>   # file list, trackers, web seeds
rom-hub torrent handoff torrents <source>   # magnet for your torrent client
rom-hub torrent fetch   torrents <source> --file <name>
```

`<source>` is any of four things:

| Input | Resolves to | Network |
|---|---|:-:|
| `myrient_nointro_SNES` — an Archive.org identifier | `torrent_url` + `info_hash` + wanted files | archive.org |
| `magnet:?xt=urn:btih:…` | `magnet`, rebuilt with declared trackers | none |
| `a4994f2d1efb0a77…` — a bare info-hash (40 hex or 32 base32) | `magnet` | none |
| `https://…/thing.torrent` | `torrent_url` | as declared |

### Worked example

```console
$ rom-hub torrent show torrents myrient_nointro_SNES
name       myrient_nointro_SNES
info_hash  a4994f2d1efb0a77a333499c63aee77261a06721
source     https://archive.org/download/myrient_nointro_SNES/myrient_nointro_SNES_archive.torrent
size       3.6 GiB in 18 file(s), 1839 piece(s) of 2.0 MiB
file    *    3.5 GiB  Super_Nintendo.zip  [sha1]
file    *   48.8 MiB  Super_Nintendo_Aftermarket.zip  [sha1]
tracker    http://bt1.archive.org:6969/announce
seed       https://archive.org/download/
note       info-hash matches the plugin's claim
```

`*` marks a file the plugin named as wanted.

## Streaming a single file

An Archive.org torrent carries https **web seeds** pointing back at archive.org, and
a per-file sha1, md5 and crc32 inside the `info` dictionary — under the info-hash.
That makes a `.torrent` a verified file manifest, so one named file can be pulled
straight over https and checked against the torrent's own digest:

```console
$ rom-hub torrent fetch torrents myrient_nointro_SNES --file Screenshot_20260301_135845.png
fetched  Screenshot_20260301_135845.png (149.0 KiB, verified sha1=7605de125d6219acf6…)
        4.05s total
```

No peers, no DHT, no torrent client — and the bytes are still checked against the
torrent rather than trusted. This is the fastest path the plugin offers, and it is
why nothing is added to an Archive.org torrent: it already has the good route.

## Any source, one currency

The manifest declares `archive.org` and nineteen trackers, and nothing else — yet
this plugin resolves torrents from indexers it has never heard of. That is not a gap
in the allowlist; it is what an info-hash *is*.

An info-hash is a digest of the torrent's `info` dictionary. It names content, not a
location: there is no host in it to check, nothing to point at an internal address,
and so nothing an allowlist has an opinion about. The host validates it for
well-formedness and stops there, because there is nothing else to validate.

So a result from Prowlarr, from a tracker, or from a list somebody pasted resolves by
its info-hash — with the manifest declaring no new host and the sandbox not loosened
by one entry.

**What it costs:** with no `.torrent` fetched there is no file manifest, so the host
cannot list what is inside until the client resolves it. When you have the `.torrent`
or an Archive.org identifier, you get the manifest as well.

## Trackers

A magnet with no `tr=` has only DHT to find peers on, which is slow to bootstrap and
blocked outright on some networks. So every magnet this plugin returns carries the
nineteen open public trackers the manifest declares, and a client can announce the
moment it starts.

The host gates each `tr=` by hostname against the manifest, which is why the list is
in both places: a tracker that is not in both does nothing.

**Incoming trackers are filtered, not passed through.** A magnet from an indexer names
trackers no static manifest could have declared, and `keep_trackers` keeps only those
already in the allowlist. The number discarded is reported in `extra.trackers_dropped`
rather than being silent — it is the cue that this magnet no longer announces where
the original one did.

A magnet naming a v2 (`urn:btmh:`) info-hash is refused: the host verifies v1 hashes,
so there would be nothing to check the download against.

## Archive.org items

The Internet Archive builds and seeds a `<identifier>_archive.torrent` for most of
what it holds and lists it in the item's own metadata with a `btih` — so the
info-hash is known without fetching the torrent, and becomes a cross-check on the
bytes the host later receives.

**Presence is tested, never inferred.** An item in `stream_only` was expected to
publish no torrent; `arcadia_Soccer_1982_UA_EU-US_aka_Football` is in `stream_only`
and publishes one. Reading the file list is the only thing that answers it.

Two refusals are ordinary outcomes rather than failures, and are named separately so
you can tell them apart:

- **No torrent published.** Usually an access-restricted item. It is still reachable
  over https.
- **Darkened.** The Archive answers `is_dark` and serves nothing — a rights
  withdrawal at the Archive's end, not a fetch that failed.

### Wanted files, and the flattening trap

`archive_files = "payload"` (the default) names the item's own originals and leaves
the Archive's derivatives — thumbnails, `_meta.xml`, `_files.xml`, `_meta.sqlite`,
the torrent itself — out of the selection. `"all"` names nothing, which means the
whole torrent.

`ia_make_torrent` **flattens** subdirectories. An item whose metadata lists
`GameCube RvZ/Animal Crossing (USA, Canada).rvz` carries it in the torrent as the
bare name, so selecting by the metadata path would name an entry the torrent does not
contain. The basename is what is there, and it is what this plugin selects.

Where two paths flatten onto the same name, that name is dropped rather than sent
twice — the host refuses a repeated selector, and one ambiguous name should not cost
you every other file in the item.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `add_trackers` | `true` | Append the declared public trackers to every magnet. Off means DHT-only. |
| `keep_trackers` | `false` | Also keep the trackers an incoming magnet carried, filtered to declared hosts. |
| `archive_files` | `"payload"` | `"payload"` selects the item's originals; `"all"` names nothing and means the whole torrent. |

```bash
rom-hub plugin config torrents
```

## Permissions

```toml
network = ["archive.org", "*.archive.org", <19 public trackers>]
```

Two different kinds of host, in one list because the host checks both against it:

- **archive.org** — fetched by the Hub: item metadata, the `.torrent`, and the https
  web seeds a single-file fetch reads from. `*.archive.org` also covers
  `bt1`/`bt2.archive.org`, whose trackers every Archive.org torrent announces to.
- **the trackers** — announced to by *your* torrent client, never fetched by the Hub.

That list is the complete set of hosts this plugin can cause traffic to. Read it
before installing.

## Terms

For an **Archive.org item**, the Internet Archive's terms, unchanged. This plugin
reaches the torrent the Archive publishes and seeds itself, for items it already
serves over https, and works around no restriction — an item the Archive will not
distribute simply has no torrent. The underlying rights are a mixed picture: the
Archive hosts public-domain, abandonware and still-copyrighted software under its own
library and DMCA position, not under a licence that passes to you.

For a **magnet or an info-hash**, the plugin makes no claim about what the torrent
contains or who may lawfully have it. It reformats an identifier you supplied. The
trackers it adds are open public ones that index nothing themselves.

---

Part of [Cartridge](https://github.com/BlizzHacker/rom-hub/blob/master/BRAND.md) by
MoveWeight — a [ROMarr](https://github.com/BlizzHacker/romarr) /
[ROM Hub](https://github.com/BlizzHacker/rom-hub) plugin.

MIT — see [LICENSE](LICENSE). Unofficial; not affiliated with the Internet Archive,
RomM, Gaseous or Retrom.
