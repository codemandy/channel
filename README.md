# Channel

Your Are.na archive as a Mac app, **CHANNEL**, and online at
**channel.innercity-life.com**, one of the innercity-life.com sites. The online
version shows only the channels you starred as favorites, and it is read-only.
Edit on the Mac and it publishes the favorites (see [Online](#online-channelinnercity-lifecom)).

It started as a one-time Are.na importer with a local browser.

## Usage

```bash
python3 arena_archive.py import maus-cats
python3 server.py
```

Then open <http://127.0.0.1:8765>.

## Mac app

`mac/build.sh` builds a native app with the free Command Line Tools
(`xcode-select --install`). You don't need Xcode or a developer account.

```bash
./mac/build.sh --install
```

On first launch, choose the folder that has `archive.db` and `assets/` (for
example this project folder). The app reads and writes those files in place
and remembers the folder. Use File › Choose Archive Folder… (⌘O) to switch.

### Updating

Push changes from the Mac where you made them. The button at the top right of
the archive window checks GitHub when you open the app (at most every 10
minutes) and when you click it. When there are new commits it turns yellow and
says how many, and clicking it runs the update. You can also choose
**CHANNEL › Update CHANNEL…**, or run:

```bash
./mac/update.sh
```

It pulls from GitHub, quits CHANNEL (which writes the archive back to iCloud),
rebuilds, reinstalls and reopens it. It stops if the project folder has
uncommitted changes.

In iCloud mode, each Mac records its build in iCloud Drive › CHANNEL ›
versions. When you open CHANNEL on a Mac with an older build than your other
Mac, it tells you and offers to update.

### Menu bar tray

The app puts a tray icon in the menu bar. Drag anything onto it: files from
Finder, images from a browser or Photos, links, or selected text. A channel
list opens under the icon:

- Drop onto a channel to file it there.
- Drop onto the icon itself to hold it, then click a channel (or search and
  press Return) to file everything that's held.

Images become image blocks. Other files become attachment blocks. Links and
text become link and text blocks. Recent channels and favorites are listed
first. The list scrolls when you hover near its top or bottom edge while
dragging.

By default the archive keeps a copy and the original file stays where it is.
Turn on "Move Files to Trash After Filing" in the tray's ⋯ menu to move
originals to the Trash instead. Closing the archive window keeps the tray
running; quit from the ⋯ menu or with ⌘Q.

### iCloud sync

To share the archive between Macs, choose **File › Move Archive to iCloud
Drive…**. It copies `archive.db` and `assets/` from your archive folder to
iCloud Drive › CHANNEL, and the app uses that copy from then on. The originals
stay where they are. On your other Mac, build and install the app, then choose
the same menu item. It finds the archive already in iCloud and uses it.

How it syncs:

- The app edits a local copy of the database in
  `~/Library/Application Support/ArenaArchive` and writes it back to iCloud
  every 20 seconds, before sleep, and on quit. SQLite files can't safely be
  synced while they're open.
- Assets are read from and written to iCloud directly. Thumbnails are a local
  cache in the same Application Support folder, so they don't sync.
- `lock.json` in the iCloud folder shows which Mac has the archive open. The
  other Mac can take over or open it read-only.
- If both Macs changed the database while out of sync, the local changes are
  saved as `archive conflict <Mac> <date>.db` next to it. Nothing is
  overwritten.

Wait for iCloud to finish syncing before opening the app on the other Mac.
To import into the synced archive, quit the app first and point the importer
at the iCloud folder:

```bash
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/CHANNEL
python3 /path/to/arena_archive.py import maus-cats --database archive.db --assets assets
```

## Online (channel.innercity-life.com)

The online Channel runs the same `server.py` as the app, read-only, on a copy
of the archive that holds only your favorite channels.

- **Publishing.** `publish.py` copies the favorite channels, their blocks and
  their files (with thumbnails) to the shared R2 bucket `innercity-life` under
  `channel/`. Nothing else leaves the Mac: other channels, nested channels that
  aren't favorites, and the raw Are.na data are left out. Unstar a channel and
  its files are removed online on the next publish.
- **When.** The app publishes after it writes a change back to iCloud (at most
  once a minute) and when it opens. **CHANNEL › Publish Favorites Online**
  publishes right away and tells you what changed. The log is in
  `~/Library/Application Support/ArenaArchive/publish.log`. From the project
  folder, `python3 publish.py` does the same from the iCloud archive.
- **Online.** `api/index.py` is a Vercel Python function that wraps
  `server.py`'s handler. It downloads `channel/archive.db` to `/tmp` and checks
  for a newer one every 30 seconds. Images and files redirect to signed R2
  links, and only for files the published database lists.
- **Login.** Every page needs the hub's passkey login: the `icl_auth` cookie,
  checked with `AUTH_PUBLIC_JWK` (`session_token.py`, from artdoc). Without
  it you go to www.innercity-life.com/login and come back after. Without
  `AUTH_PUBLIC_JWK` the site answers 503.

### Deploy

It runs on the Vercel project `channel`, connected to `codemandy/channel`, so
pushing to `main` deploys. The domain is `channel.innercity-life.com` (a CNAME
at Namecheap to the target `vercel domains verify channel.innercity-life.com`
shows). The `*.vercel.app` addresses are behind Vercel's own login; the domain
is behind the hub's.

### Set up from scratch

```bash
vercel link --yes --project channel
```

```bash
curl -s https://www.innercity-life.com/api/public-key | vercel env add AUTH_PUBLIC_JWK production
```

```bash
./scripts/set-r2-keys.sh
```

`set-r2-keys.sh` asks for the R2 account, bucket and key (the archive's,
artdoc's and Texts' token works), sets them on the Vercel project, writes them
to `.env.local` for `publish.py`, and deploys. On a second Mac that should
publish too, run `./scripts/set-r2-keys.sh --local-only`. Then add the domain
`channel.innercity-life.com` to the Vercel project (a CNAME at Namecheap to
the target Vercel shows) and publish once:

```bash
python3 publish.py
```

`.vercelignore` uploads only the code (`api/`, `server.py`, `style.css`,
`r2.py`, `session_token.py`). The archive reaches the site only through R2.

### Try it locally

```bash
python3 publish.py --out /tmp/channel-store
```

```bash
CHANNEL_STORE=/tmp/channel-store python3 api/index.py
```

Then open <http://127.0.0.1:8770>. Locally it runs without the login.

## Tests

```bash
python3 -m unittest
```

`test_online.py` checks the R2 signatures against AWS's examples, that only
favorites are published, and the online site. Its login test needs
`cryptography` and is skipped without it.

## Importer

The importer stores `archive.db` and downloaded files under `assets/`. Set
`ARENA_TOKEN` enables importing channels visible to that account, including
private channels. Keep the resulting archive local.

Images dropped into a channel are stored under
`assets/channels/<channel-id>/`. Browsers provide a copy of a local file to a
web app, so the original file on your computer is not moved or deleted.

The API importer spaces requests and retries HTTP 429 responses using
Are.na's `Retry-After` value. With a personal read token, it uses the V3
`scope=my` search to find your full channel set, including private channels.
Without a token, it falls back to publicly discoverable, non-private channels
owned by the profile:

```bash
ARENA_TOKEN=your_read_token python3 arena_archive.py import maus-cats
```

Useful options:

```bash
python3 arena_archive.py import maus-cats --database my-archive.db --assets my-assets
python3 arena_archive.py import maus-cats --fixture fixtures/profile.json
```

The fixture option is intended for development and tests. API requests are
paginated and limited to the account's owned channels and their first-level
contents; nested channel contents are not traversed.
