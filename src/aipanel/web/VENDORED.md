# Vendored files

Kept byte for byte as upstream published them; check the hash after an update.

| File | Upstream | Version | SHA-256 | License |
| --- | --- | --- | --- | --- |
| `htmx.min.js` | [htmx](https://github.com/bigskysoftware/htmx) release asset `htmx.min.js` (also `dist/htmx.min.js` of the npm package `htmx.org`) | 2.0.11 | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` | Zero-Clause BSD, `htmx.LICENSE` |

Update: download the new release's `htmx.min.js`, compare it with the npm package's copy, put
its version and hash here and in `tests/test_vendored.py`, and change `?v=` in
`templates/base.html`; the files are in `static/`.
