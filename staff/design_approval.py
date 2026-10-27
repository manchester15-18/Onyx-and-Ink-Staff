"""Design approval + quality-reference system (Oct 27, 2026).

Purpose: keep track of which designs are approved, verified, and high-quality,
and mark the best ones as REFERENCES — the standard future designs are judged
against for quality/standard control.

Statuses (one design, one current status):
- pending: generated, not yet reviewed.
- verified: vision PASS + checklist PASS + critique pass (agent/human eye).
  Eligible for approval, but NOT yet a reference standard on its own.
- approved: verified + a named approver signed off. The approver is recorded.
  Self-approval (an agent approving its own design) is recorded as such and
  is weaker QC than an independent approver — James's sign-off is the true
  production standard, and he can revoke any approval.
- reference: approved + marked as the standard for its product. Only
  references are used for comparison and for prompt notation.
- rejected: failed QC — wrong product, AI text, off-brand, or otherwise
  unpublishable. Rejected designs are never references and never used as
  generation examples.
- revoked: previously approved/reference, later withdrawn. The record stays
  (audit trail), the design stops being a reference.

What is stored per design (work/design-approvals.json):
- id: first 12 chars of the file's SHA-256 (stable, detects file replacement).
- image_path (relative to project root when possible), product, agent (generator),
  approver, approved_at_utc, status, reference (bool), verified_by (list:
  e.g. vision, checklist, human_eye, critique), vision_verdict, critique_verdict,
  text_found, dimensions, file_hash, notation, notes, tags.
- notation: the quality note that makes this a standard, e.g.
  "Standard for keychain: round, black face with gold trim, diamond accent at
  top, metal ring, blank face for personalization, no text, black/gold luxe."
  Reference notation is returned by reference_notation(product) and appended
  to learned generation prompts, so future generations are steered toward the
  standard, not just away from past failures.

Comparison: compare_to_reference() (in staff/vision_inspect.py, or via the
CrewAI compare tool) shows the vision model BOTH the reference image and the
candidate image and asks whether the candidate meets the reference standard,
with differences listed. That is the quality-control check, not a text match.

Storage: a single JSON file, written atomically (temp file + rename) to
avoid corruption if the dashboard and a CrewAI run write at the same time.
The file lives in work/ (git-ignored, like the learning state) — back it up
before any PC wipe.
"""
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("pending", "verified", "approved", "reference", "rejected", "revoked")


def approvals_path(root):
    return Path(root) / "work" / "design-approvals.json"


def load_approvals(root):
    path = approvals_path(root)
    if not path.exists():
        return {"records": []}
    try:
        data = json.loads(path.read_text())
        data.setdefault("records", [])
        return data
    except Exception:
        return {"records": []}


def _save_approvals(root, data):
    path = approvals_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2)
        tmp = f.name
    Path(tmp).replace(path)


def _file_info(root, image_path):
    src = Path(image_path)
    if not src.is_absolute():
        src = Path(root) / src
    if not src.exists():
        raise FileNotFoundError(f"Design file not found: {src}")
    raw = src.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    dimensions = None
    try:
        from PIL import Image
        with Image.open(src) as im:
            dimensions = f"{im.width}x{im.height}"
    except Exception:
        pass
    return src, file_hash, dimensions


def _relative(root, src):
    try:
        return str(src.relative_to(Path(root)))
    except Exception:
        return str(src)


def set_approval(root, image_path, product, status, approver="", agent="", verified_by=None,
                 vision_verdict=None, critique_verdict=None, text_found=False,
                 notation="", notes="", tags=None, reference=False):
    """Create or update an approval record. Returns the record.

    Setting status="reference" also requires the design to be approved-grade:
    the caller must have verified it first — this function records what the
    caller claims, it does not re-verify the image. Callers should only set
    reference=True after vision + checklist + critique pass, or human approval.
    Revoking: call with status="revoked" — the old record is kept, reference
    is cleared, and the design stops being a standard.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    src, file_hash, dimensions = _file_info(root, image_path)
    data = load_approvals(root)
    record_id = file_hash[:12]
    record = None
    for r in data["records"]:
        if r["id"] == record_id:
            record = r
            break
    if record is None:
        record = {"id": record_id, "history": []}
        data["records"].append(record)
    record["history"].append({
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "from_status": record.get("status"),
        "to_status": status,
        "approver": approver,
        "notes": notes,
    })
    record.update({
        "image_path": _relative(root, src),
        "product": str(product),
        "agent": agent,
        "approver": approver,
        "approved_at_utc": datetime.now(timezone.utc).isoformat() if status in ("approved", "reference") else record.get("approved_at_utc"),
        "status": status,
        "reference": bool(reference) and status == "reference",
        "verified_by": verified_by or [],
        "vision_verdict": vision_verdict,
        "critique_verdict": critique_verdict,
        "text_found": bool(text_found),
        "dimensions": dimensions,
        "file_hash": file_hash,
        "notation": notation,
        "notes": notes,
        "tags": tags or [],
    })
    _save_approvals(root, data)
    return record


def approve_design(root, image_path, product, approver, agent="", verified_by=None,
                   vision_verdict=None, critique_verdict=None, text_found=False,
                   notation="", notes="", tags=None, as_reference=False):
    """Approve a design. as_reference=True marks it as a quality standard."""
    status = "reference" if as_reference else "approved"
    return set_approval(root, image_path, product, status, approver=approver, agent=agent,
                        verified_by=verified_by, vision_verdict=vision_verdict,
                        critique_verdict=critique_verdict, text_found=text_found,
                        notation=notation, notes=notes, tags=tags, reference=as_reference)


def reject_design(root, image_path, product, approver="", agent="", notes="", tags=None):
    """Mark a design rejected — never a reference, never a generation example."""
    return set_approval(root, image_path, product, "rejected", approver=approver, agent=agent,
                        notes=notes, tags=tags, reference=False)


def revoke_approval(root, image_path, approver="", notes=""):
    """Revoke a prior approval/reference. Record stays, reference stops."""
    data = load_approvals(root)
    src, file_hash, _ = _file_info(root, image_path)
    for r in data["records"]:
        if r["id"] == file_hash[:12]:
            return set_approval(root, image_path, r["product"], "revoked", approver=approver,
                                agent=r.get("agent", ""), notes=notes or "Approval revoked.", reference=False)
    raise ValueError("No approval record found for that image.")


def list_approvals(root, product=None, status=None, reference_only=False):
    records = load_approvals(root)["records"]
    out = []
    for r in records:
        if product and r.get("product", "").lower() != str(product).lower():
            continue
        if status and r.get("status") != status:
            continue
        if reference_only and not r.get("reference"):
            continue
        out.append(r)
    return out


def get_references(root, product=None):
    return list_approvals(root, product=product, reference_only=True)


def is_approved(root, image_path):
    try:
        _, file_hash, _ = _file_info(root, image_path)
    except Exception:
        return False
    for r in load_approvals(root)["records"]:
        if r["id"] == file_hash[:12] and r.get("status") in ("approved", "reference"):
            return True
    return False


def reference_notation(root, product):
    """Combined notation of all reference standards for a product, or ''."""
    refs = get_references(root, product)
    notations = [r["notation"] for r in refs if r.get("notation")]
    if not notations:
        return ""
    return " Quality reference standard(s) for " + str(product) + ": " + " | ".join(notations)
