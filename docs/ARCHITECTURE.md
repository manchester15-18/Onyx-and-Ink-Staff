# Architecture

## Entry points

- `main.py` runs a bounded staff assignment.
- `dashboard.py` serves the local dashboard.
- `inbox_monitor.py` receives and routes staff mail.

## Application services

Reusable Python services live in `staff/`:

- `agent_*`, `staff_*`, and `model_routing.py` manage agent conversations, assignments, autonomy, email, and cloud routing.
- `dashboard_*`, `inbox_*`, and `google_mail_auth.py` support the dashboard and mail workflow.
- `report_*` owns the company report format, project records, and exports.
- `design_images.py`, `multimodal.py`, `web_research.py`, `workspace_tools.py`, `square_commerce.py`, and `storefront.py` are external capability adapters.

## Operational folders

- `dashboard/` is the browser interface.
- `deploy/` contains Windows and hosted deployment scripts.
- `reports/` holds project outputs.
- `work/` holds local runtime state and is not application source.
- `tests/` contains automated checks.
- `docs/` holds reference material and rollout notes.
- `cloudflare-storefront/` is the separate public website repository.