"""Install the signup/tracking/back-in-stock snippet in a store's LIVE Shopify theme: upload
snippets/om-signup.liquid + one render line before </body>.

    python ops/theme_signup.py shop.example.com            # dry run
    python ops/theme_signup.py shop.example.com --apply    # backup layout → ops/backups/ (gitignored), then write
    python ops/theme_signup.py shop.example.com --rollback # remove the render line (snippet file stays, unused)

Needs the store's app credentials in the env (stores.py client_id_env / client_secret_env); the app needs the
read_themes + write_themes scopes. Try the snippet on an unpublished theme copy first (?preview_theme_id=).
"""
import os
import pathlib
import sys

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config  # noqa: E402
import shopify  # noqa: E402
import stores  # noqa: E402

LINE = "{% render 'om-signup' %}"


def _gql(store):
    """A GraphQL caller for the store's Admin API, with the same app credentials as the functions (env vars named in
    stores.py: client_id_env / client_secret_env)."""
    token = shopify.access_token(store, os.environ[store.client_id_env].strip(), os.environ[store.client_secret_env].strip())
    url = f"https://{store.shop}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    def gql(q, v=None):
        j = requests.post(url, headers=h, json={"query": q, "variables": v or {}}, timeout=60).json()
        if j.get("errors"):
            sys.exit(f"GraphQL errors: {j['errors']}")
        return j["data"]
    return gql


def main(argv):
    store = stores.get(argv[0])
    mode = (argv[1:] or ["--dry"])[0]
    gql = _gql(store)

    def read(theme, name):
        n = gql("""query($id:ID!,$f:[String!]){theme(id:$id){files(filenames:$f){nodes{
          body{... on OnlineStoreThemeFileBodyText{content}}}}}}""", {"id": theme, "f": [name]})["theme"]["files"]["nodes"]
        return n[0]["body"]["content"] if n else None

    def write(theme, files):
        d = gql("""mutation($id:ID!,$files:[OnlineStoreThemeFilesUpsertFileInput!]!){themeFilesUpsert(themeId:$id, files:$files){
          upsertedThemeFiles{filename} userErrors{field message}}}""",
                {"id": theme, "files": [{"filename": k, "body": {"type": "TEXT", "value": v}} for k, v in files.items()]})["themeFilesUpsert"]
        if d["userErrors"]:
            sys.exit(f"userErrors: {d['userErrors']}")
        print("written:", [f["filename"] for f in d["upsertedThemeFiles"]])

    main_theme = [t for t in gql("{themes(first:50){nodes{id name role}}}")["themes"]["nodes"] if t["role"] == "MAIN"][0]
    theme = main_theme["id"]
    layout = read(theme, "layout/theme.liquid")
    print("LIVE theme:", main_theme["name"], theme, "| render line present:", LINE in layout)
    if mode == "--rollback":
        if LINE in layout:
            write(theme, {"layout/theme.liquid": layout.replace(LINE + "\n", "").replace(LINE, "")})
        print("render line present now:", LINE in read(theme, "layout/theme.liquid"))
        return
    snippet = (HERE.parent / "theme" / "om-signup.liquid").read_text()
    if mode != "--apply":
        print("DRY RUN — would upload the snippet and", "keep" if LINE in layout else "insert", "the render line")
        return
    backup = HERE / "backups" / f"theme_liquid_backup_{theme.rsplit('/', 1)[1]}.liquid"
    backup.parent.mkdir(exist_ok=True)
    if not backup.exists():
        backup.write_text(layout)
    write(theme, {"snippets/om-signup.liquid": snippet})
    if LINE not in layout:
        i = layout.rindex("</body>")
        write(theme, {"layout/theme.liquid": layout[:i] + LINE + "\n" + layout[i:]})
    print("readback: render line once:", read(theme, "layout/theme.liquid").count(LINE) == 1,
          "| snippet matches:", read(theme, "snippets/om-signup.liquid") == snippet, "| backup:", backup.name)


if __name__ == "__main__":
    main(sys.argv[1:])
