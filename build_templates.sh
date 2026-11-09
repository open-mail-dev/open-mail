#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# deploy.sh rebuilds templates/base.html from templates/base.mjml before every deploy.
# Pin the exact mjml version (not a floating "mjml@4") so a rebuild can never
# silently produce a different base.html than the one compiled and reviewed here.
# 4.18.0 is the version that produced the committed templates/base.html.
npx --yes mjml@4.18.0 templates/base.mjml -o templates/base.html

# MJML url-encodes the mj-button href attribute (e.g. turns {{ cta_url }} into
# %7B%7B%20cta_url%20%7D%7D), which breaks the Jinja placeholder. Undo that
# encoding for our known placeholders only, so the rest of the compiled HTML
# (any legitimately-encoded content) is left untouched.
python3 - <<'PY'
import pathlib

p = pathlib.Path("templates/base.html")
html = p.read_text()
for name in (
    "preheader", "brand", "tagline", "usps_html", "body_html", "cta_url", "cta_label",
    "help_html", "footer_links_html", "footer", "unsubscribe_url", "unsubscribe_label",
):
    mangled = "%7B%7B%20" + name + "%20%7D%7D"
    html = html.replace(mangled, "{{ " + name + " }}")
p.write_text(html)
PY
