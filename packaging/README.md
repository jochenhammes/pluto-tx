# packaging/ — the pluto-tx .deb

One binary package `pluto-tx` per target (docs/RELEASE_PLAN.md), built in
a container of the target distribution:

```bash
packaging/build-deb.sh                 # ubuntu26.04 and deb13, native architecture
packaging/build-deb.sh deb13           # one target
packaging/test-install.sh ubuntu26.04 packaging/out/pluto-tx_*~ubuntu26.04_amd64.deb
```

Needs podman (or docker) and git; Node.js on the host is optional (else a
`node:22` container builds the Web-TRX frontend). The result lands in
`packaging/out/` together with `SHA256SUMS` and the build logs incl. lintian.
The build takes the working tree (uncommitted changes mark the version with
`+dirty`).

| File | Role |
|---|---|
| `components.lock` | pinned third-party versions, also read by the `install-*.sh` scripts |
| `build-components.sh` | builds gr-m17, gr-lora_sdr, rade_c, ft8_lib, js8ref + jsc.json into a staging tree (RPATH `/usr/lib/pluto-tx/lib`) |
| `build-deb.sh` | frontend once, then per target `dpkg-buildpackage -b` + lintian in `container/Containerfile.build` |
| `test-install.sh` | install test in a fresh container: apt resolves the dependencies from the distribution alone, smoke tests, Web-TRX, the full unittest suite against the installed code, remove/purge |
| `make-sources.sh` | `sources-<version>.tar.xz`: pluto-tx and every bundled component at its pinned commit (GPL corresponding source for a release) |
| `debian/` | debhelper packaging; `extra/` holds launchers, udev rule, modprobe blacklist, `web-trx@.service`, desktop files, icons, man pages, AppStream metainfo |

Downloads and clones are cached in `packaging/.cache/<target>/` (the
RADE/Opus model alone is 176 MB).

## Installed layout

```
/usr/bin/{pluto-tx,pluto-advanced-rx,pluto-cli,web-trx,pluto-tx-doctor}
/usr/share/pluto-tx/        Python code (as in a checkout, web-trx/ next to pluto_tx/), .pluto-tx-package marker
/usr/share/pluto-tx/js8/    jsc.json
/usr/lib/pluto-tx/lib|bin|python   native components, private
/usr/lib/udev/rules.d/60-pluto-tx.rules, /usr/lib/modprobe.d/pluto-tx-blacklist-rtl.conf
/usr/lib/systemd/system/web-trx@.service   (disabled; `web-trx service enable`)
```

Per user: `~/.config/pluto-tx/web-trx.env`, `~/.local/state/web-trx/`,
`~/.cache/pluto-tx/`. Resource lookup and these paths: `pluto_tx/paths.py`.
