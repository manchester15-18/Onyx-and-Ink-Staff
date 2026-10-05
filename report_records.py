"""Report metadata, safe exports, and reference-document archiving."""
import fcntl
import hashlib
import json
import re
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

REFERENCE_TERMS = (
    "terms and conditions", "terms of service", "return policy", "refund policy",
    "privacy policy", "company policy", "internal policy", "employee handbook",
    "code of conduct", "standard operating procedure", "procedure", "compliance",
    "workplace rule", "leave policy", "safety policy", "legal guidance",
)


def report_path(root, identifier):
    report_root = (Path(root) / "reports").resolve()
    path = (report_root / str(identifier)).resolve()
    if not path.is_relative_to(report_root) or path.suffix.lower() != ".md" or not path.is_file():
        raise ValueError("Choose a saved report.")
    return path


def assignment_name(path, body=None):
    text = body if body is not None else Path(path).read_text()
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if match:
        return re.sub(r"[*_`]", "", match.group(1)).strip()[:160]
    stem = Path(path).stem.rsplit("-", 1)[0]
    return stem.replace("_", " ").replace("-", " ").title()[:160]


def _registry_path(root):
    return Path(root) / "work" / "report-metadata.json"


def _load(root):
    try:
        data = json.loads(_registry_path(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(root, data):
    path = _registry_path(root);path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n");temporary.chmod(0o600);temporary.replace(path)


def metadata(root, path):
    root = Path(root).resolve();path = Path(path).resolve();key = str(path.relative_to(root / "reports"))
    stat = path.stat();body = path.read_text()
    created_stamp = getattr(stat, "st_birthtime", stat.st_ctime)
    lock_path = _registry_path(root).with_suffix(".lock");lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(root);item = data.get(key, {}) if isinstance(data.get(key), dict) else {}
        before=dict(item)
        item["title"] = assignment_name(path, body)
        item.setdefault("created", datetime.fromtimestamp(created_stamp, timezone.utc).isoformat())
        item["edited"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        if item!=before:data[key] = item;_save(root, data)
    return dict(item)


def is_reference_report(title, body):
    haystack = (str(title) + "\n" + str(body)).lower()
    return any(term in haystack for term in REFERENCE_TERMS)


def _document_text(title, meta, body):
    created = datetime.fromisoformat(meta["created"]).astimezone().strftime("%B %-d, %Y at %-I:%M %p")
    edited = datetime.fromisoformat(meta["edited"]).astimezone().strftime("%B %-d, %Y at %-I:%M %p")
    return f"{title}\n\nCreated: {created}\nLast edited: {edited}\n\n{body.strip()}\n"


def export_google_doc(root, identifier, force=False):
    """Create or update one app-owned Google Doc for a report."""
    root=Path(root).resolve();path = report_path(root, identifier);body = path.read_text();meta = metadata(root, path);title = meta["title"]
    if not force and not is_reference_report(title, body):
        return None
    digest = hashlib.sha256(body.encode()).hexdigest();key = str(path.relative_to(Path(root) / "reports"))
    if meta.get("googleDigest") == digest and meta.get("googleUrl"):
        return {"id": meta.get("googleId", ""), "url": meta["googleUrl"], "unchanged": True}
    from workspace_tools import Workspace
    workspace = Workspace(root);identifier_doc = meta.get("googleId")
    text = _document_text(title, meta, body)
    if identifier_doc:
        try:
            workspace.replace_doc(identifier_doc, text);result = {"id": identifier_doc, "url": meta["googleUrl"]}
        except (RuntimeError, ValueError):
            result = workspace.create_doc(title, text)
    else:
        result = workspace.create_doc(title, text)
    lock_path = _registry_path(root).with_suffix(".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX);data = _load(root);item = data.get(key, meta)
        item.update({"googleId": result["id"], "googleUrl": result["url"], "googleDigest": digest,
                     "googleExported": datetime.now(timezone.utc).isoformat(), "googleStatus": "exported"})
        data[key] = item;_save(root, data)
    return result


def auto_export_reference(root, path):
    try:
        resolved_root=Path(root).resolve()
        relative = str(Path(path).resolve().relative_to(resolved_root / "reports"))
        return export_google_doc(resolved_root, relative, force=False)
    except (RuntimeError, ValueError, OSError):
        return None


def pdf_bytes(title, meta, markdown):
    """Build a small standards-compliant PDF without adding a runtime dependency."""
    plain = re.sub(r"!\[[^]]*\]\([^)]+\)", "", markdown)
    plain = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", plain)
    plain = re.sub(r"^#{1,6}\s+", "", plain, flags=re.M)
    plain = re.sub(r"[*_`]", "", plain)
    stamp = lambda value: datetime.fromisoformat(value).astimezone().strftime("%b %-d, %Y %-I:%M %p")
    lines = [title, "Created: " + stamp(meta["created"]), "Last edited: " + stamp(meta["edited"]), ""]
    for raw in plain.splitlines():
        lines.extend(textwrap.wrap(raw, width=92, replace_whitespace=False) or [""])
    chunks = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [[title]]
    objects = [None, None]
    font_id = len(objects) + 1;objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids=[]
    for chunk in chunks:
        content = ["BT /F1 10 Tf 50 760 Td 14 TL"]
        for line in chunk:
            safe = line.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.append("(" + safe + ") Tj T*")
        content.append("ET");stream = "\n".join(content).encode("latin-1")
        content_id=len(objects)+1;objects.append(b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream")
        page_id=len(objects)+1;page_ids.append(page_id)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode())
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [".encode()+b" ".join(f"{i} 0 R".encode() for i in page_ids)+b"] >>"
    output=bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n");offsets=[0]
    for number,obj in enumerate(objects,1):
        offsets.append(len(output));output.extend(f"{number} 0 obj\n".encode()+obj+b"\nendobj\n")
    start=len(output);output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(output)


def report_pdf(root, identifier):
    path = report_path(root, identifier);meta = metadata(root, path)
    return meta["title"], pdf_bytes(meta["title"], meta, path.read_text())
