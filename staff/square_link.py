"""Link existing storefront products to Square production catalog items.

James's directive (Oct 30, 2026): ALL products must be Square-linked —
the seven made-to-order pieces and the Lucky Sister bundle included,
not just the four catalog items. This tool stages that backfill; it
never applies it (the host applier does, preview-only, for
CEO-approved ready jobs).

Per slug, linking means all of:
- a Square production catalog ITEM with the exact catalogue name and a
  priced Regular variation, plus an exact stock count via physical
  count (verified by read-back);
- a ``squareId`` line in the products.js entry (which also flips the
  piece to direct Square checkout, since canCheckout keys on squareId —
  personalization is then confirmed by email after the order, as the
  bundle already works);
- a server-map entry in functions/api/_square.js keyed by the slug
  (preserved verbatim when one already exists, e.g. the bundle's
  customPrice entry, so checkout behaviour for that piece does not
  change).

One-of-one pieces (one_of_one) must carry stock 1 and are labeled with
the 'One of One' badge, a first details line 'One of one — sold as
is.', and a oneOfOne entry field.

Sequencing is enforced, not assumed: the site patch is staged only
after the Square steps succeed (publish mode + CEO approval + a
production token). Without those, the run stages the patches with
status staged_pending_approval and touches nothing in Square — a site
patch must never go live before its Square items exist, or checkout
for those pieces would fail with "needs a price in Square". The
applier refuses a link job whose live files changed since staging
(base sha256 guard), so a stale patch can never clobber newer edits.

The catalogue is parsed with node (exact, not regex) and the staged
files pass node --check before the job is written.
"""

import hashlib
import json
import re
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SQUARE_ANCHOR = "\n};\nexport const apiFor"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _live_storefront(root):
    for candidate in (Path(root) / "cloudflare-storefront", Path(root) / "storefront"):
        if (candidate / "products.js").is_file():
            return candidate
    return None


def _load_catalogue(products_text):
    """Parse products.js exactly via node (the file is a script, not JSON)."""
    script = "global.window = {};\n" + products_text + "\nconsole.log(JSON.stringify(window.ONYX.catalogue));\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "parse.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Could not parse products.js: " + (result.stderr or "")[:300])
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("Could not parse products.js output.")
    if not isinstance(data, list):
        raise RuntimeError("products.js catalogue did not parse to a list.")
    return data


def _node_check(products_path, square_text):
    result = subprocess.run(["node", "--check", str(products_path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("products.js syntax check failed: " + (result.stderr or "")[:300])
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "package.json").write_text('{"type":"module"}', encoding="utf-8")
        square_path = Path(tmp) / "_square.js"
        square_path.write_text(square_text, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(square_path)],
                                capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Server map syntax check failed: " + (result.stderr or "")[:300])


def _entry_span(text, slug):
    match = re.search(r"(?m)^      slug: '" + re.escape(slug) + r"',$", text)
    if not match:
        raise ValueError("Slug %s not found in products.js." % slug)
    start = match.start()
    rest = text[start:]
    ends = []
    found = re.search(r"\n    \},", rest)
    if found:
        ends.append(start + found.start() + len("\n    },"))
    found = re.search(r"\n    \}\n  \];", rest)
    if found:
        ends.append(start + found.start() + len("\n    }"))
    if not ends:
        raise ValueError("Entry %s has no closing brace." % slug)
    return start, min(ends)


def _patch_entry(text, slug, one_of_one):
    start, end = _entry_span(text, slug)
    span = text[start:end]
    span = re.sub(r"(?m)^(      slug: '" + re.escape(slug) + r"',)$",
                  r"\1\n      squareId: '" + slug + r"',", span, count=1)
    if one_of_one:
        span = re.sub(r"(?m)^(      squareId: '" + re.escape(slug) + r"',)$",
                      r"\1\n      oneOfOne: true,", span, count=1)
        if re.search(r"(?m)^      badge: '.*',$", span):
            span = re.sub(r"(?m)^      badge: '.*',$",
                          "      badge: 'One of One',", span, count=1)
        else:
            span = re.sub(r"(?m)^(      name: '.*',)$",
                          r"\1\n      badge: 'One of One',", span, count=1)
        details = re.search(r"details: \[\n", span)
        if not details:
            raise ValueError("Entry %s has no details list." % slug)
        rest = span[details.end():]
        first_item = re.search(r"(?m)^        '.*',?$", rest)
        if not first_item:
            raise ValueError("Entry %s details list is empty." % slug)
        pos = details.end() + first_item.start()
        span = span[:pos] + "        'One of one — sold as is.',\n" + span[pos:]
    return text[:start] + span + text[end:]


def _js_escape_single(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _map_has_slug(server_text, slug):
    return re.search(r"['\"]" + re.escape(slug) + r"['\"]\s*:\s*\{", server_text) is not None


def _write_manifest(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def link_existing_products(root, spec):
    """Stage a Square-link backfill for existing catalogue slugs."""
    root = Path(root)
    if not isinstance(spec, dict):
        raise ValueError("Link spec must be an object.")
    items = spec.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError("Provide items as a list of 1-20 slugs.")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each link item must be an object.")
        slug = item.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug.strip()):
            raise ValueError("Provide a valid lowercase slug per item.")
        stock = item.get("stock_quantity")
        if not _is_int(stock) or not 0 <= stock <= 100000:
            raise ValueError("Provide stock_quantity as a whole number from 0 to 100000.")
        one_of_one = item.get("one_of_one") is True
        if one_of_one and stock != 1:
            raise ValueError("One-of-one items must have stock_quantity 1.")
        normalized.append({"slug": slug.strip(), "stock_quantity": stock,
                           "one_of_one": one_of_one})
    mode = spec.get("mode", "stage")
    if mode not in ("stage", "publish"):
        raise ValueError("Use mode stage or publish.")
    ceo_approved = spec.get("ceo_approved") is True
    location_id = spec.get("location_id", "")
    if location_id is not None and not isinstance(location_id, str):
        raise ValueError("Provide location_id as text.")

    site = _live_storefront(root)
    if site is None:
        raise ValueError("The live storefront folder was not found.")
    products_path = site / "products.js"
    square_path = site / "functions" / "api" / "_square.js"
    products_raw = products_path.read_bytes()
    square_raw = square_path.read_bytes()
    # The live server map uses CRLF; normalize for the regex patching
    # below, then restore each file's own endings when staging, so a
    # link commit does not rewrite every line of the file.
    products_crlf = b"\r\n" in products_raw
    square_crlf = b"\r\n" in square_raw
    products_text = products_raw.decode("utf-8").replace("\r\n", "\n")
    server_text = square_raw.decode("utf-8").replace("\r\n", "\n")
    catalogue = _load_catalogue(products_text)
    by_slug = {entry.get("slug"): entry for entry in catalogue if isinstance(entry, dict)}

    stamp = datetime.now(BUSINESS_TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    job_dir = root / "work" / "product-jobs" / ("link-" + stamp)
    manifest_path = job_dir / "manifest.json"
    now = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    base = {"kind": "link", "mode": mode, "ceo_approved": ceo_approved,
            "created": now, "updated": now, "applied_commit": None, "preview_url": None}
    try:
        linked, skipped = [], []
        square_results = {}
        publish = mode == "publish" and ceo_approved
        for item in normalized:
            entry = by_slug.get(item["slug"])
            if entry is None:
                raise ValueError("Slug %s is not in the catalogue." % item["slug"])
            had_map = _map_has_slug(server_text, item["slug"])
            already = bool(entry.get("squareId")) and had_map
            if already:
                skipped.append(item["slug"])
                continue
            if publish:
                from staff.product_pipeline import _square_publish
                square_results[item["slug"]] = _square_publish(root, {
                    "slug": item["slug"], "name": entry["name"],
                    "summary": entry.get("summary", entry["name"]),
                    "price_cents": int(entry["price"]),
                    "stock_quantity": item["stock_quantity"],
                    "location_id": (location_id or "").strip()})
            products_text = _patch_entry(products_text, item["slug"], item["one_of_one"])
            if not had_map:
                if _SQUARE_ANCHOR not in server_text:
                    raise RuntimeError("Server Square map anchor not found.")
                # The previous last map entry has no trailing comma; add
                # one, or the inserted entry is a syntax error.
                head, tail = server_text.split(_SQUARE_ANCHOR, 1)
                head = head.rstrip()
                if not head.endswith(","):
                    head += ","
                map_entry = "  '%s':{name:'%s'},\n" % (
                    item["slug"], _js_escape_single(entry["name"]))
                server_text = head + "\n" + map_entry + _SQUARE_ANCHOR + tail
            linked.append({"slug": item["slug"], "stock_quantity": item["stock_quantity"],
                           "one_of_one": item["one_of_one"], "map_preserved": had_map})
        if not linked:
            return {"result": "All requested slugs are already Square-linked; no job staged.",
                    "kind": "link", "status": "nothing_to_do", "skipped": skipped}
        # Guard: no catalogue entry lost in patching.
        staged_slugs = {entry.get("slug") for entry in _load_catalogue(products_text)}
        if staged_slugs != set(by_slug):
            raise RuntimeError("Staged catalogue slug set changed during patching.")
        job_dir.mkdir(parents=True, exist_ok=True)
        staged_products = job_dir / "products.js"
        staged_square = job_dir / "_square.js"
        staged_products_text = products_text.replace("\n", "\r\n") if products_crlf else products_text
        staged_square_text = server_text.replace("\n", "\r\n") if square_crlf else server_text
        staged_products.write_text(staged_products_text, encoding="utf-8", newline="")
        staged_square.write_text(staged_square_text, encoding="utf-8", newline="")
        _node_check(staged_products, server_text)
        if publish:
            status = "ready_to_apply"
            note = ("Square production items and stock verified; link patch staged "
                    "for the host applier (preview-only push).")
        elif mode == "publish":
            status = "staged_pending_approval"
            note = ("Square and site-push steps skipped: publish mode needs CEO approval. "
                    "Link patch is staged.")
        else:
            status = "staged"
            note = "Stage mode: link patch staged; Square untouched."
        manifest = dict(base, status=status, note=note, slugs=[item["slug"] for item in linked],
                        linked=linked, skipped=skipped, square=square_results,
                        base_shas={"products.js": hashlib.sha256(
                            products_path.read_bytes()).hexdigest(),
                            "_square.js": hashlib.sha256(
                                square_path.read_bytes()).hexdigest()})
        _write_manifest(manifest_path, manifest)
        return {"result": note, "kind": "link", "status": status,
                "manifest": str(manifest_path), "linked": linked, "skipped": skipped,
                "square": square_results}
    except Exception as error:
        try:
            _write_manifest(manifest_path, dict(base, status="failed",
                                                note="Link run failed before completion.",
                                                error=str(error)[:300]))
        except (OSError, ValueError):
            pass
        raise
