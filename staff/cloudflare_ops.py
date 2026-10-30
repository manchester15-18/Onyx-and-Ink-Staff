"""Bounded Cloudflare D1 + Pages operations for Jordan (Oct 30, 2026).

Why this exists: the launch project needs a production D1 database
created, bound to the Pages production environment, and migrated — but
staff agents had no Cloudflare tool at all. The API token already in
the PC .env (used for Workers AI) reads D1 and Pages fine; this module
gives Jordan the write path, tightly bounded:

* D1 databases only, and only names starting with ``onyx-`` (the
  production database is ``onyx-ink-prod``). No delete action exists.
* D1 queries only: SELECT, CREATE TABLE, CREATE INDEX. Anything else
  (DROP, DELETE, UPDATE, ALTER, PRAGMA, multi-statement tricks) is
  refused before any API call, so an agent can migrate a schema but
  cannot wipe or rewrite data.
* Pages bindings only for the project in CLOUDFLARE_PAGES_PROJECT
  (corrected Oct 30, 2026 to ``onyx-and-ink`` — the env var previously
  held the subdomain prefix ``onyx-and-ink-site``, which is not a
  project and 404s). Binding merges into the project's existing
  deployment_configs and verifies afterwards that every other
  environment's bindings are unchanged; on mismatch it restores the
  original configs once and reports the failure.
* No DNS, zone, worker-route, or account operations of any kind.

The token itself is never logged or returned by any method here.
"""

import copy
import re
from pathlib import Path

import requests
from dotenv import dotenv_values

_NAME_RE = re.compile(r"^onyx-[a-z0-9][a-z0-9\-]{1,62}$")
_VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_ALLOWED = ("SELECT ", "CREATE TABLE ", "CREATE INDEX ", "CREATE UNIQUE INDEX ")
_ENVIRONMENTS = ("production", "preview")


def validate_database_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        raise ValueError("Use an Onyx D1 database name: onyx- followed by lowercase letters, digits, or hyphens.")
    return name.strip()


def validate_sql(sql):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Provide one SQL statement.")
    statement = sql.strip()
    if not statement.upper().startswith(_SQL_ALLOWED):
        raise ValueError("D1 queries are limited to SELECT, CREATE TABLE, and CREATE INDEX. No data changes or drops.")
    if ";" in statement.rstrip(";"):
        raise ValueError("Run one statement at a time.")
    return statement


def _binding_id(value):
    if isinstance(value, dict):
        return value.get("id", "")
    return value or ""


class CloudflareOps:
    def __init__(self, root):
        self.root = Path(root)
        cfg = dotenv_values(self.root / ".env")
        self.token = cfg.get("CLOUDFLARE_API_TOKEN", "").strip()
        self.account = cfg.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        self.project = cfg.get("CLOUDFLARE_PAGES_PROJECT", "").strip()
        self.base = "https://api.cloudflare.com/client/v4"

    @property
    def configured(self):
        return bool(self.token and self.account)

    def _request(self, method, path, payload=None):
        if not self.configured:
            raise RuntimeError("Connect Cloudflare in Settings before managing D1 or Pages.")
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        response = requests.request(method, self.base + path, headers=headers,
                                    json=payload, timeout=30)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.ok:
            errors = data.get("errors") or [{}]
            detail = errors[0].get("message", "") if isinstance(errors, list) else ""
            raise RuntimeError("Cloudflare request failed" + (": " + detail if detail else ".") +
                               " (HTTP %d)" % response.status_code)
        return data.get("result", data)

    def list_databases(self):
        result = self._request("GET", "/accounts/%s/d1/database?per_page=100" % self.account)
        databases = result if isinstance(result, list) else []
        return {"databases": [{"uuid": d.get("uuid", ""), "name": d.get("name", ""),
                               "created_at": d.get("created_at", "")} for d in databases]}

    def resolve_database(self, name_or_uuid):
        value = str(name_or_uuid or "").strip()
        if re.fullmatch(r"[0-9a-fA-F\-]{36}", value):
            result = self._request("GET", "/accounts/%s/d1/database/%s" % (self.account, value))
            return {"uuid": result.get("uuid", value), "name": result.get("name", "")}
        for database in self.list_databases()["databases"]:
            if database["name"] == value:
                return database
        raise ValueError("No D1 database named %s exists." % value)

    def create_database(self, name):
        name = validate_database_name(name)
        for database in self.list_databases()["databases"]:
            if database["name"] == name:
                return {"database": database, "created": False,
                        "note": "A database with this name already exists; no duplicate was created."}
        result = self._request("POST", "/accounts/%s/d1/database" % self.account, {"name": name})
        return {"database": {"uuid": result.get("uuid", ""), "name": result.get("name", name),
                             "created_at": result.get("created_at", "")}, "created": True}

    def query(self, database, sql):
        statement = validate_sql(sql)
        resolved = self.resolve_database(database)
        result = self._request("POST", "/accounts/%s/d1/database/%s/query" % (self.account, resolved["uuid"]),
                               {"sql": statement})
        first = result[0] if isinstance(result, list) and result else {}
        return {"database": resolved, "results": first.get("results", []),
                "meta": first.get("meta", {}), "success": first.get("success", True)}

    def list_projects(self):
        result = self._request("GET", "/accounts/%s/pages/projects" % self.account)
        projects = result if isinstance(result, list) else []
        return {"projects": [{"name": p.get("name", ""), "domains": p.get("domains", [])} for p in projects]}

    def get_project(self, name=""):
        project = (name or self.project).strip()
        if not project:
            raise ValueError("No Cloudflare Pages project is configured.")
        result = self._request("GET", "/accounts/%s/pages/projects/%s" % (self.account, project))
        configs = result.get("deployment_configs") or {}
        bindings = {}
        for environment in _ENVIRONMENTS:
            bindings[environment] = dict((configs.get(environment) or {}).get("d1_databases") or {})
        return {"name": result.get("name", project), "domains": result.get("domains", []),
                "d1_databases": bindings}

    def bind_d1(self, database, environment="production", variable="DB", project=""):
        project = (project or self.project).strip()
        if environment not in _ENVIRONMENTS:
            raise ValueError("Bind D1 to the production or preview environment only.")
        if not _VARIABLE_RE.match(str(variable or "")):
            raise ValueError("Use a plain binding variable name such as DB.")
        resolved = self.resolve_database(database)
        current = self.get_project(project)
        original = self._raw_configs(project)
        merged = copy.deepcopy(original)
        # Binding values are objects {"id": uuid}, not bare UUID strings —
        # a bare string is rejected with HTTP 400.
        merged.setdefault(environment, {}).setdefault("d1_databases", {})[variable] = {"id": resolved["uuid"]}
        self._request("PATCH", "/accounts/%s/pages/projects/%s" % (self.account, project),
                      {"deployment_configs": merged})
        after = self.get_project(project)
        preserved = all(
            {k: _binding_id(v) for k, v in after["d1_databases"][env].items()}
            == {k: _binding_id(v) for k, v in current["d1_databases"][env].items()}
            for env in _ENVIRONMENTS if env != environment)
        bound = _binding_id(after["d1_databases"][environment].get(variable)) == resolved["uuid"]
        if not bound or not preserved:
            try:
                self._request("PATCH", "/accounts/%s/pages/projects/%s" % (self.account, project),
                              {"deployment_configs": original})
            except Exception:
                pass
            raise RuntimeError("Pages binding verification failed; the original bindings were restored. "
                               "Production binding was not changed.")
        return {"project": project, "environment": environment, "variable": variable,
                "database": resolved, "before": current["d1_databases"][environment],
                "after": after["d1_databases"][environment], "other_environments_preserved": preserved}

    def _raw_configs(self, project):
        result = self._request("GET", "/accounts/%s/pages/projects/%s" % (self.account, project))
        return copy.deepcopy(result.get("deployment_configs") or {})
