# Aegis Platform — Baseline Metrics Report (Phase 1 Baseline)

This document establishes the baseline metrics for the Aegis Platform after Phase 1 (Bounded Contexts & Data Consolidation) completion. These metrics will serve as a reference point for optimization comparison in subsequent development phases.

---

## 1. Build & Dependency Resolution Performance

* **Prior State**: The service used an unpinned `requirements.txt` containing conflicting dependencies (specifically `protobuf==7.35.1` conflicting with `google-generativeai` requiring `protobuf<6.0.0dev`). This triggered recursive backtracking in `pip`, resulting in container builds taking **several minutes or timing out**.
* **Optimized State**: Indirect and direct sub-dependencies were fully resolved and pinned to compatible versions in `requirements.lock` (re-resolving to `protobuf==5.29.6`).
* **Optimized Build Time**: A clean container build now completes in **~17.5 seconds** (a **90%+ speedup**).

---

## 2. Containerized Resource Footprint (`docker stats`)

Resource utilization metrics captured in an idle state:

| Service Container | CPU % | Memory Usage | Memory % | Process Count (PIDs) |
| :--- | :--- | :--- | :--- | :--- |
| **`e2ee_messenger`** | 0.15% | 55.79 MiB | 0.70% | 2 |
| **`sre_agent`** | 0.00% | 94.53 MiB | 1.19% | 2 |
| **`reeftracker_app`** | 0.93% | 121.80 MiB | 1.53% | 3 |
| **`shariahscreener`** | 0.25% | 39.11 MiB | 0.49% | 4 |
| **`aegis_db`** (Postgres 15) | 0.00% | 98.89 MiB | 1.25% | 16 |
| **Total Overhead** | **1.33%** | **410.12 MiB** | — | **27** |

---

## 3. Database Consolidation Footprint

* **Architecture**: Deprecated separate flat-file storage (SQLite `halal_screener.db` files, SQLite Django databases, and flat JSON logs) across microservices. Unified and consolidated state into **4 logically isolated schemas** (`db_sre`, `db_screener`, `db_reeftracker`, `db_e2ee_messenger`) within a single PostgreSQL 15 container.
* **Storage Footprint**: **112.2 MB** total physical size of the Postgres data directory.

---

## 4. Codebase Size & Quality

* **Codebase Volume**: **12,133 Lines of Code (LOC)** across all active python services (excluding external libraries, virtual environments, and caches).
* **Test Suite Quality**: **93 tests** executed and **100% passing** inside the `shariahscreener` container environment.
* **Test Suite Runtime**: **196.17 seconds (3m 16s)**.
