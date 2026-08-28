# dehli-musikk-sample-plugins

The development workspace for the [Dehli Musikk](https://store.dehlimusikk.no/) sample plugins. It ties together the shared sampler engine, the DecentSampler converter, the desktop authoring app and every plugin product into a single CMake build, so the whole family of instruments compiles, tests, runs and packages from one place.

Each sub project is its own git repository, co located here as siblings so they share one JUCE fetch and one build tree. This root repository only versions the orchestration: the top level CMake, the `dmse` workflow CLI and the packaging scripts. The sub repositories themselves are not tracked here.

## What lives here

* `CMakeLists.txt` fetches JUCE once and adds every sub project. It sets universal macOS binaries (arm64 plus x86_64), a default Release build type and the shared `dmse_add_plugin` helper from the engine.
* `dmse` is a single entry point for the workflow, so neither you nor a tool has to remember the raw cmake, converter and packaging incantations.
* `packaging/` holds the macOS signing and notarization scripts, the Linux tarball builder and the shell completion for `dmse`.
* `site/` holds the product page generator and its stylesheet. It renders each plugin repository's README into the `docs/` folder that GitHub Pages serves.

The sub repositories, each pulled in through `add_subdirectory`, are:

* [`dehli-musikk-sampler-engine`](https://github.com/benjamindehli/dehli-musikk-sampler-engine), the shared JUCE sampler engine that loads a JSON manifest plus a FLAC sample bundle and renders it as audio and a data driven UI. Every plugin is a thin wrapper around it.
* [`ds-plugin-converter`](https://github.com/benjamindehli/ds-plugin-converter), a build time CLI that translates a DecentSampler library into the engine's manifest and asset bundle.
* [`dehli-musikk-sampler-plugin-editor`](https://github.com/benjamindehli/dehli-musikk-sampler-plugin-editor), "DMSE Studio", a desktop app for authoring and editing plugins against the real engine.
* The 13 plugin products, one per sample library: Omni-84, Maskintrommer, Midnight Wurli, Elektrisk Salmesykkel, EDB-Orgel, Strykebrett, StyloPoly, SubC, Lo-fi Tape Piano, Voltage Controlled Cassette Organ, and the 4-track Glockenspiel, Toy Piano and Music Box.

## The dmse CLI

Run everything through `./dmse`. A plugin name is matched loosely against its folder, target and product name, so `omni`, `omni-84` and `Omni84` all resolve to the same plugin, and `all` acts on every plugin.

```
./dmse list                 # every plugin and the name you can pass
./dmse convert omni-84      # DecentSampler/ into assets/ (reconvert)
./dmse build omni-84        # build the Standalone (fast), or "all" for AU + VST3 + Standalone
./dmse run omni-84          # build and launch the Standalone
./dmse test                 # build and run the engine and converter test suites
./dmse format               # reformat all C++ to LLVM style, or --check to just report
./dmse package omni-84      # sign and notarize the macOS .pkg (needs signing config)
./dmse tarball omni-84      # build the Linux .tar.gz
./dmse site omni-84         # render the product page into the plugin's docs/ folder
./dmse configure            # (re)run cmake, also done automatically when needed
```

The build directory defaults to `build`. Override it with the `BUILD_DIR` environment variable, for example `BUILD_DIR=build-linux` for a Linux build. Shell completion for commands, plugin names and build kinds is in `packaging/dmse-completion.sh`. Source it from your shell profile.

## Building without the CLI

The workspace is a normal CMake superproject if you prefer raw commands:

```
cmake -B build
cmake --build build --target Omni84_Standalone
ctest --test-dir build
```

JUCE 8 is fetched automatically. Keep the build path free of parentheses and spaces, since JUCE's plugin manifest and binary data steps mis quote them.

## Product pages

Every plugin repository publishes a product page through GitHub Pages, for example [benjamindehli.github.io/Omni-84](https://benjamindehli.github.io/Omni-84/). The page is generated, never hand written:

```
./dmse site omni-84         # one plugin
./dmse site all             # all 13
```

`site/generate_site.py` reads the plugin's `README.md` and renders it as a designed page: a hero with the icon, the latest version and a link to the store, the largest screenshot as the lead image, the demo video, the full control documentation with every screenshot, the equipment gallery, the release history as a collapsible list, and a strip of links to the other twelve instruments. The README stays the single source of truth, so a page is refreshed by editing the README and running the command again. Never hand edit a `docs/` folder, it is overwritten on the next run.

What a README cannot express lives in `site/plugin-data.json`, one entry per product, matched to the plugin by title: the store link, the price, the `sameAs` profiles (Gumroad, Cylex, Pianobook, YouTube), the demo video and, where the automatic choice is wrong, a `heroImage`. The lead image is otherwise the largest screenshot, which is right whenever one shot is the full interface, but EDB-Orgel has three equally sized tab screenshots and names its mixer tab explicitly. The store sells pay what you want, so a price is written as a minimum:

```json
"price": { "minimum": "9.99", "currency": "USD", "payWhatYouWant": true }
```

which the page shows as "From $9.99" and publishes as both `offers.price` and a `minPrice`, so the figure is not read as a fixed one. A minimum of `0` renders as "Free · pay what you want" and adds `isAccessibleForFree`. The `jsonLdIds` are what tie the pages into the wider web: `product` is the instrument's identity on dehlimusikk.no, so that page, this one and the store listing describe one entity rather than three, and `website` identifies the GitHub Pages site itself. The `website` id must be the repository's real Pages URL, and the generator says so if the two disagree. Alongside them each page publishes the author as a `Person` keyed by MusicBrainz and the publisher as an `Organization` keyed by dehlimusikk.no, so the same author and label resolve across every product. The video is embedded as a click to load facade, so no request reaches YouTube until the reader presses play. `video` may also be a list, in which case the page shows a grid of players.

Each video also gets a watch page of its own, at `video/` and then `video/2/`, `video/3/` for any further ones. Google only indexes a video when it is the main content of the page and when the player is real markup rather than something a click creates, so the watch page leads with an ordinary `<iframe>` and carries the `VideoObject` structured data, while the product page keeps the facade and simply links to it. The generated `sitemap.xml` lists the product page plus every watch page, the latter with the video sitemap extension.

Each plugin's README links to its published page just under the heading. The generator drops that link when it renders the page, since a page does not need to link to itself.

Screenshots are re encoded for the web at up to 1200 pixels wide, as WebP when `cwebp` or Pillow is available and otherwise as JPEG through `sips`, which ships with macOS. A page and all its images come to a few hundred kilobytes rather than the ten megabytes the raw screenshots would cost. The originals in `Screenshots/` are never touched.

The pages are written for search engines: a unique title and description per product, canonical, Open Graph and video tags, schema.org `SoftwareApplication` and `VideoObject` data with the version, operating systems, screenshots and `sameAs` profiles, a sitemap, internal links between all thirteen products, lazily loaded images with explicit dimensions and a preloaded hero image.

To publish a plugin for the first time, commit its `docs/` folder, then in the repository settings under Pages choose "Deploy from a branch", branch `main`, folder `/docs`.

A single plugin can override the shared data with an optional `site.json` in its repository root. Every key is optional and takes precedence over `site/plugin-data.json`:

```json
{
  "storeUrl": "https://store.dehlimusikk.no/l/omni-84",
  "tagline": "One line pitch, overrides the README's first sentence",
  "heroImage": "Screenshots/Keyboard.png",
  "price": "29",
  "currency": "USD"
}
```

A `price` here may be a plain number or the same object as in the shared data. Note that a project page's `robots.txt` only applies at the domain root, so the generated sitemaps are best submitted to Search Console directly, or listed from the `benjamindehli.github.io` user site.

## Packaging and distribution

The plugins are paid products. Their sample audio, images and impulse responses are never committed to any repository, so a fresh clone re runs the converter from a local copy of each DecentSampler library. macOS builds are signed and notarized through `./dmse package`, which reads the signing identities from `packaging/signing.env` (gitignored). Linux tarballs are built with `./dmse tarball`. Windows installers are planned.

The finished plugins are available from [store.dehlimusikk.no](https://store.dehlimusikk.no/).
