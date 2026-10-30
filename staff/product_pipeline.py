"""Create one fully linked product: images, SEO, site listing, Square.

The pipeline is deterministic. Everything it cannot verify, it refuses —
it never invents a price, a stock quantity, copy, or an image:

1. Validate the spec. Price, stock, summary, description, details,
   keywords, and exactly 5 images (prompts or files) are all required
   inputs from the approved task. There are no defaults for price or
   stock.
2. Images: 5 prompts generate through Cloudflare Workers AI Flux and
   convert to WebP (quality 85, dimensions unchanged, per repo policy);
   or 5 supplied image files convert the same way. A failed generation
   fails the run — no placeholder image is ever staged.
3. Site listing patch: a products.js catalogue entry (slug, squareId,
   hero image, 5-image gallery, alt, summary, description, details,
   SEO keywords) plus the matching functions/api/_square.js map entry.
   The patch is STAGED under work/product-jobs/<slug>/, not applied to
   the repo here — the dashboard container mounts the storefront
   read-only, so the host applier applies it and pushes preview only.
4. Square (publish mode only, production only): create or update the
   catalog ITEM by exact product name with a priced Regular variation,
   then set exact stock with a physical count and verify the read-back.
   Refuses outright unless SQUARE_ENVIRONMENT is production with a
   token configured. Publish mode without CEO approval downgrades to
   stage-only and records why; nothing reaches Square.
5. Manifest: work/product-jobs/<slug>/manifest.json is the single
   record the host applier consumes and the next cycle reports from.
   It links the site slug to the Square item id, variation id, and
   verified stock — that linkage is what "listed on the store" means
   here: the listing, the server map entry, and the Square item all
   resolve to the same product and price.

Checkout linkage, for the record: the site's server code resolves a
cart line by the _square.js map key (the slug) and then by exact
Square catalog item NAME, taking the first variation's price. The
pipeline guarantees all three agree: slug == map key == squareId, and
Square item name == catalogue name.

One-of-one items (one_of_one) must carry stock 1 and are labeled with
the 'One of One' badge, a first details line 'One of one — sold as
is.', and a oneOfOne entry field. Made-to-order items (made_to_order)
are linked the same way; linking flips them to direct Square checkout
(canCheckout keys on squareId), with personalization confirmed by
email after the order.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_IMAGE_BYTES = 15_000_000


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(spec, key, minimum, maximum):
    value = spec.get(key)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError("Provide %s as text of %d-%d characters." % (key, minimum, maximum))
    return value.strip()


def _require_text_list(spec, key, min_items, max_items, item_min, item_max):
    values = spec.get(key)
    if (not isinstance(values, list) or not min_items <= len(values) <= max_items
            or any(not isinstance(item, str) or not item_min <= len(item.strip()) <= item_max
                   for item in values)):
        raise ValueError("Provide %s as %d-%d items of %d-%d characters each."
                         % (key, min_items, max_items, item_min, item_max))
    return [item.strip() for item in values]


def _validate_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("Product spec must be an object.")
    slug = _require_text(spec, "slug", 1, 60)
    if not _SLUG_RE.match(slug):
        raise ValueError("Use a lowercase slug with letters, digits, and single hyphens.")
    name = _require_text(spec, "name", 3, 50)
    category = _require_text(spec, "category", 3, 60)
    price_cents = spec.get("price_cents")
    if not _is_int(price_cents) or not 100 <= price_cents <= 100000:
        raise ValueError("Provide price_cents as a whole number of cents from 100 to 100000.")
    stock = spec.get("stock_quantity")
    if not _is_int(stock) or not 0 <= stock <= 100000:
        raise ValueError("Provide stock_quantity as a whole number from 0 to 100000.")
    summary = _require_text(spec, "summary", 50, 160)
    description = _require_text_list(spec, "description", 1, 4, 10, 1000)
    details = _require_text_list(spec, "details", 1, 8, 1, 120)
    keywords = _require_text_list(spec, "keywords", 3, 10, 2, 40)
    prompts = spec.get("image_prompts")
    files = spec.get("image_files")
    if (prompts is None) == (files is None):
        raise ValueError("Provide exactly one of image_prompts or image_files, with exactly 5 entries.")
    if prompts is not None:
        prompts = _require_text_list(spec, "image_prompts", 5, 5, 10, 500)
    else:
        files = _require_text_list(spec, "image_files", 5, 5, 1, 500)
        checked = []
        for raw in files:
            path = Path(raw)
            if not path.is_file():
                raise ValueError("Image file not found: %s" % raw)
            if path.suffix.lower() not in _IMAGE_SUFFIXES:
                raise ValueError("Image files must be PNG, JPEG, or WebP: %s" % raw)
            if path.stat().st_size > _MAX_IMAGE_BYTES:
                raise ValueError("Image file exceeds 15 MB: %s" % raw)
            checked.append(path)
        files = checked
    alts = spec.get("alts")
    if alts is not None:
        alts = _require_text_list(spec, "alts", 5, 5, 10, 200)
    else:
        alts = ["%s example %d" % (name, index + 1) for index in range(5)]
    options = spec.get("options") or []
    if not isinstance(options, list) or len(options) > 8:
        raise ValueError("Provide options as a list of at most 8 groups.")
    normalized_options = []
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("Each option must be an object.")
        option_id = option.get("id")
        if not isinstance(option_id, str) or not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", option_id.strip()) or len(option_id.strip()) > 40:
            raise ValueError("Each option needs a lowercase slug id of at most 40 characters.")
        label = option.get("label")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
            raise ValueError("Each option needs a label of 1-60 characters.")
        option_type = option.get("type")
        if option_type not in ("radio", "select", "text"):
            raise ValueError("Option type must be radio, select, or text.")
        normalized = {"id": option_id.strip(), "label": label.strip(), "type": option_type,
                      "required": option.get("required") is True}
        if option_type in ("radio", "select"):
            raw_choices = option.get("choices")
            if not isinstance(raw_choices, list) or not 2 <= len(raw_choices) <= 20:
                raise ValueError("Radio and dropdown options need 2-20 choices.")
            choices = []
            for choice in raw_choices:
                value = choice.get("value") if isinstance(choice, dict) else choice
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
                    raise ValueError("Each choice needs a value of 1-80 characters.")
                choices.append({"value": value.strip()})
            if len({choice["value"] for choice in choices}) != len(choices):
                raise ValueError("Option choices must be unique.")
            normalized["choices"] = choices
        else:
            max_length = option.get("maxLength", 100)
            if not _is_int(max_length) or not 1 <= max_length <= 200:
                raise ValueError("Text options need maxLength from 1 to 200.")
            normalized["maxLength"] = max_length
            placeholder = option.get("placeholder")
            if placeholder is not None:
                if not isinstance(placeholder, str) or len(placeholder) > 80:
                    raise ValueError("Option placeholder must be at most 80 characters.")
                normalized["placeholder"] = placeholder
        normalized_options.append(normalized)
    if len({option["id"] for option in normalized_options}) != len(normalized_options):
        raise ValueError("Option ids must be unique.")
    if spec.get("made_to_order") is True:
        # Allowed: a made-to-order piece is Square-linked like any other
        # product (catalog item + server map + squareId). Linking flips it
        # to direct Square checkout (canCheckout keys on squareId), with
        # personalization confirmed by email after the order, as the
        # bundle already works. Its Square count is the spec's number.
        pass
    badge = spec.get("badge")
    if badge is not None and (not isinstance(badge, str) or not 1 <= len(badge.strip()) <= 20):
        raise ValueError("Provide badge as text of 1-20 characters.")
    one_of_one = spec.get("one_of_one") is True
    if one_of_one and stock != 1:
        raise ValueError("One-of-one items must have stock_quantity 1.")
    if one_of_one:
        if badge is not None and badge.strip() != "One of One":
            raise ValueError("One-of-one items carry the badge 'One of One'.")
        badge = "One of One"
    mode = spec.get("mode", "stage")
    if mode not in ("stage", "publish"):
        raise ValueError("Use mode stage or publish.")
    location_id = spec.get("location_id", "")
    if location_id is not None and not isinstance(location_id, str):
        raise ValueError("Provide location_id as text.")
    return {
        "slug": slug, "name": name, "category": category,
        "price_cents": price_cents, "stock_quantity": stock,
        "summary": summary, "description": description, "details": details,
        "keywords": keywords, "image_prompts": prompts, "image_files": files,
        "alts": alts, "badge": badge.strip() if badge else None,
        "featured": spec.get("featured") is True,
        "mode": mode, "ceo_approved": spec.get("ceo_approved") is True,
        "location_id": (location_id or "").strip(),
        "one_of_one": one_of_one,
        "made_to_order": spec.get("made_to_order") is True,
        "options": normalized_options,
    }


def _live_storefront(root):
    for candidate in (Path(root) / "cloudflare-storefront", Path(root) / "storefront"):
        if (candidate / "products.js").is_file():
            return candidate
    return None


def _check_slug_free(root, slug):
    site = _live_storefront(root)
    if site is None:
        return
    catalogue = (site / "products.js").read_text(encoding="utf-8")
    if re.search(r"slug:\s*['\"]" + re.escape(slug) + r"['\"]", catalogue):
        raise ValueError("Slug %s is already listed in products.js." % slug)
    server_map = site / "functions" / "api" / "_square.js"
    if server_map.is_file():
        text = server_map.read_text(encoding="utf-8")
        if re.search(r"['\"]" + re.escape(slug) + r"['\"]\s*:\s*\{\s*name", text):
            raise ValueError("Slug %s is already in the server Square map." % slug)


def _to_webp(source_bytes, destination):
    from PIL import Image
    import io
    with Image.open(io.BytesIO(source_bytes)) as image:
        image.load()
        rgb = image.convert("RGB")
        rgb.save(str(destination), format="WEBP", quality=85)


def _make_images(root, data, job_dir):
    assets_dir = job_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    filenames = [data["slug"] + ".webp"] + [
        "%s-view-%d.webp" % (data["slug"], index) for index in range(2, 6)]
    site_paths = []
    if data["image_prompts"] is not None:
        from staff.design_images import generate
        for prompt, filename in zip(data["image_prompts"], filenames):
            png = generate(root, prompt, "schnell")
            _to_webp(png, assets_dir / filename)
            site_paths.append("/assets/" + filename)
    else:
        for path, filename in zip(data["image_files"], filenames):
            _to_webp(path.read_bytes(), assets_dir / filename)
            site_paths.append("/assets/" + filename)
    return site_paths


def _build_entry(data, site_paths):
    details = (["One of one — sold as is."] + data["details"]) if data["one_of_one"] else data["details"]
    entry = {
        "slug": data["slug"], "name": data["name"], "category": data["category"],
        "price": data["price_cents"], "squareId": data["slug"],
        "image": site_paths[0], "gallery": site_paths, "alt": data["alts"][0],
        "summary": data["summary"], "description": data["description"],
        "details": details,
        "seo": {"title": data["name"] + " | Onyx & Ink",
                "description": data["summary"], "keywords": data["keywords"]},
    }
    if data["badge"]:
        entry["badge"] = data["badge"]
    if data["featured"]:
        entry["featured"] = True
    if data["options"]:
        entry["options"] = data["options"]
    if data["one_of_one"]:
        entry["oneOfOne"] = True
    if data["made_to_order"]:
        entry["madeToOrder"] = True
    return entry


def _square_publish(root, data):
    from staff.square_commerce import SquareCommerce
    square = SquareCommerce(root)
    if not square.configured:
        raise RuntimeError("Connect Square in Settings before publishing products.")
    if square.mode != "production":
        raise RuntimeError(
            "Square is in sandbox mode. Production catalog items need a production "
            "access token and SQUARE_ENVIRONMENT=production; refusing a sandbox item.")
    published = square.publish_product(
        {"id": data["slug"], "title": data["name"], "description": data["summary"],
         "price": data["price_cents"] / 100}, None)
    item_id = (published or {}).get("square_item_id", "")
    if not item_id:
        raise RuntimeError("Square did not return a catalog item ID.")
    variation = square.find_variation(data["name"])
    inventory = square.set_inventory(data["name"], data["stock_quantity"], data["location_id"])
    if inventory.get("after") != data["stock_quantity"]:
        raise RuntimeError("Square stock verification failed: read-back %s, expected %s."
                           % (inventory.get("after"), data["stock_quantity"]))
    return {"square_item_id": item_id, "variation_id": variation.get("variation_id", ""),
            "variation_name": variation.get("variation_name", ""),
            "location_id": inventory.get("location_id", ""),
            "stock_before": inventory.get("before"),
            "stock_after": inventory.get("after"), "environment": "production"}


def _write_manifest(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def create_product(root, spec):
    """Run the pipeline for one product spec. Returns the manifest summary."""
    root = Path(root)
    data = _validate_spec(spec)
    job_dir = root / "work" / "product-jobs" / data["slug"]
    manifest_path = job_dir / "manifest.json"
    _check_slug_free(root, data["slug"])
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if existing.get("status") in ("ready_to_apply", "applied"):
            raise ValueError("Product job %s already exists with status %s."
                             % (data["slug"], existing.get("status")))
    now = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    base = {"slug": data["slug"], "name": data["name"], "category": data["category"],
            "price_cents": data["price_cents"], "stock_quantity": data["stock_quantity"],
            "mode": data["mode"], "ceo_approved": data["ceo_approved"],
            "one_of_one": data["one_of_one"], "made_to_order": data["made_to_order"],
            "created": now, "updated": now, "applied_commit": None, "preview_url": None}
    try:
        site_paths = _make_images(root, data, job_dir)
        entry = _build_entry(data, site_paths)
        square = None
        if data["mode"] == "publish" and not data["ceo_approved"]:
            status = "staged_pending_approval"
            note = ("Square and site-push steps skipped: publish mode needs CEO approval. "
                    "Images and listing patch are staged.")
        elif data["mode"] == "publish":
            square = _square_publish(root, data)
            status = "ready_to_apply"
            note = ("Square production item created and stock verified; listing patch staged "
                    "for the host applier (preview-only push).")
        else:
            status = "staged"
            note = "Stage mode: images and listing patch staged; Square untouched."
        manifest = dict(base, status=status, note=note, entry=entry, images=site_paths,
                        square=square, alts=data["alts"])
        _write_manifest(manifest_path, manifest)
        return {"result": note, "slug": data["slug"], "status": status,
                "manifest": str(manifest_path), "entry": entry,
                "square_item_id": (square or {}).get("square_item_id"),
                "stock_after": (square or {}).get("stock_after"),
                "images": site_paths}
    except Exception as error:
        try:
            _write_manifest(manifest_path, dict(base, status="failed",
                                                note="Pipeline failed before completion.",
                                                error=str(error)[:300], entry=None,
                                                images=[], square=None, alts=data["alts"]))
        except (OSError, ValueError):
            pass
        raise
