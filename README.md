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

`site/generate_site.py` reads the plugin's `README.md` and renders it as a designed page: a hero with the icon, the latest version and a link to the store, the largest screenshot as the lead image, the full control documentation with every screenshot, the equipment gallery, and the release history as a collapsible list. The README stays the single source of truth, so a page is refreshed by editing the README and running the command again. Never hand edit a `docs/` folder, it is overwritten on the next run.

Screenshots are re encoded for the web at up to 1200 pixels wide, as WebP when `cwebp` or Pillow is available and otherwise as JPEG through `sips`, which ships with macOS. A page and all its images come to a few hundred kilobytes rather than the ten megabytes the raw screenshots would cost. The originals in `Screenshots/` are never touched.

The pages are written for search engines: a unique title and description per product, canonical and Open Graph tags, schema.org `SoftwareApplication` data with the version, operating systems and screenshots, a sitemap, lazily loaded images with explicit dimensions and a preloaded hero image.

To publish a plugin for the first time, commit its `docs/` folder, then in the repository settings under Pages choose "Deploy from a branch", branch `main`, folder `/docs`.

Anything the README cannot express goes in an optional `site.json` in the plugin repository root. Every key is optional:

```json
{
  "storeUrl": "https://store.dehlimusikk.no/l/omni-84",
  "tagline": "One line pitch, overrides the README's first sentence",
  "heroImage": "Screenshots/Keyboard.png",
  "price": "29",
  "currency": "USD"
}
```

A per product `storeUrl` sends buyers straight to the product instead of the store front page, and a price completes the schema.org offer. Note that a project page's `robots.txt` only applies at the domain root, so the generated sitemaps are best submitted to Search Console directly, or listed from the `benjamindehli.github.io` user site.

## Packaging and distribution

The plugins are paid products. Their sample audio, images and impulse responses are never committed to any repository, so a fresh clone re runs the converter from a local copy of each DecentSampler library. macOS builds are signed and notarized through `./dmse package`, which reads the signing identities from `packaging/signing.env` (gitignored). Linux tarballs are built with `./dmse tarball`. Windows installers are planned.

The finished plugins are available from [store.dehlimusikk.no](https://store.dehlimusikk.no/).
